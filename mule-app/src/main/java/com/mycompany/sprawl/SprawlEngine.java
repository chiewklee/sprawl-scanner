package com.mycompany.sprawl;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;

import java.time.Instant;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * Entry points invoked from Mule flows (Java module, invoke-static). Everything crosses the
 * boundary as JSON strings: Mule owns persistence (Object Store), this class owns the scan.
 *
 * Config JSON: anypoint{baseUrl, clientId, clientSecret, orgId}, gemini{apiKey, embedModel,
 * judgeModel}, thresholds{redundant, gray}, preferredAssets[], workers, plus the scan request
 * (types[], liveMcp, judge, judgeModel, judgeLimit).
 */
public final class SprawlEngine {
    private static final Map<String, ObjectNode> PROGRESS = new ConcurrentHashMap<>();

    static final String PROMPT = """
            You are auditing an enterprise integration catalog for duplicated capabilities.
            Two capabilities from different assets scored as semantically similar. Decide:
            - "duplicate": they do the same job; one could replace the other.
            - "overlap": they share part of the job but each does something the other does not.
            - "distinct": they only look similar; different purpose or business object.

            A (%s '%s'): %s: %s
            B (%s '%s'): %s: %s

            Reply as JSON: {"verdict": "duplicate|overlap|distinct", "rationale": "<one sentence>"}""";

    /** Live progress of a running scan in this replica, or "null". */
    public static String progress(String scanId) {
        ObjectNode p = PROGRESS.get(scanId);
        return p == null ? "null" : p.toString();
    }

    private static void phase(String scanId, String phase, int done, int total) {
        ObjectNode p = Http.JSON.createObjectNode();
        p.put("phase", phase).put("done", done).put("total", total);
        PROGRESS.put(scanId, p);
    }

    /** Runs a full scan. Returns {"result": {...}, "embedCache": {...}, "judgeCache": {...}};
     *  the caches are the inputs plus anything learned in this run. */
    public static String scan(String configJson, String embedCacheJson, String judgeCacheJson, String scanId)
            throws Exception {
        long started = System.currentTimeMillis();
        JsonNode cfg = readTree(configJson);
        try {
            Map<String, String> embedCache = readMap(embedCacheJson);
            Map<String, JsonNode> judgeCache = new HashMap<>();
            readTree(judgeCacheJson).fields().forEachRemaining(e -> judgeCache.put(e.getKey(), e.getValue()));

            JsonNode any = cfg.path("anypoint");
            Exchange ex = new Exchange(any.path("baseUrl").asText("https://anypoint.mulesoft.com"),
                    any.path("clientId").asText(), any.path("clientSecret").asText(), any.path("orgId").asText(""));
            for (JsonNode a : cfg.path("mcpAuth")) {
                if (!a.path("clientId").asText("").isBlank() && !a.path("clientSecret").asText("").isBlank()) {
                    List<String> hosts = new ArrayList<>();
                    a.path("hosts").forEach(h -> hosts.add(h.asText()));
                    ex.addAuthRule(new Exchange.AuthRule(a.path("name").asText("oauth"), hosts, a.path("tokenUrl").asText(),
                            a.path("clientId").asText(), a.path("clientSecret").asText()));
                }
            }
            List<String> types = new ArrayList<>();
            cfg.path("types").forEach(t -> types.add(t.asText()));
            AtomicInteger done = new AtomicInteger(), total = new AtomicInteger();
            phase(scanId, "ingesting", 0, 0);
            Exchange.Result ing = ex.scan(types, cfg.path("liveMcp").asBoolean(true), cfg.path("workers").asInt(8),
                    t -> {
                        total.set(t);
                        phase(scanId, "ingesting", 0, t);
                    },
                    () -> phase(scanId, "ingesting", done.incrementAndGet(), total.get()));
            if (ing.caps.isEmpty()) {
                throw new IllegalStateException("no capabilities ingested from Exchange");
            }

            JsonNode gem = cfg.path("gemini");
            Gemini g = new Gemini(gem.path("apiKey").asText());
            String embedModel = gem.path("embedModel").asText("gemini-embedding-001");
            phase(scanId, "embedding", 0, ing.caps.size());
            List<String> texts = ing.caps.stream().map(Cap::text).toList();
            float[][] vec = g.embed(embedModel, texts, embedCache);

            phase(scanId, "analysing", 0, 0);
            double red = cfg.path("thresholds").path("redundant").asDouble(0.91);
            double gray = cfg.path("thresholds").path("gray").asDouble(0.88);
            Set<String> preferred = new HashSet<>();
            cfg.path("preferredAssets").forEach(p -> preferred.add(p.asText()));
            Analyzer.Analysis an = Analyzer.analyze(ing.caps, vec, red, gray, preferred);

            String judgeModel = cfg.path("judgeModel").asText("");
            if (judgeModel.isBlank()) {
                judgeModel = gem.path("judgeModel").asText("gemini-3.8-flash");
            }
            int[] judgeStats = {0, 0};
            if (cfg.path("judge").asBoolean(true)) {
                // Requests-per-minute pacing: explicit request value, else per-model default from config.
                int modelDefault = judgeModel.contains("pro") ? gem.path("proRpm").asInt(20) : gem.path("flashRpm").asInt(0);
                int rpm = cfg.path("judgeRpm").isNumber() ? cfg.path("judgeRpm").asInt() : modelDefault; // null = default
                judgeStats = judge(g, judgeModel, an, cfg.path("judgeLimit").asInt(400), rpm, judgeCache, scanId);
            }

            phase(scanId, "storing", 0, 0);
            ObjectNode result = render(scanId, an, ing, embedModel, cfg.path("judge").asBoolean(true) ? judgeModel : null,
                    red, gray, judgeStats, System.currentTimeMillis() - started);
            ObjectNode out = Http.JSON.createObjectNode();
            out.set("result", result);
            out.set("embedCache", Http.JSON.valueToTree(embedCache));
            out.set("judgeCache", Http.JSON.valueToTree(judgeCache));
            return out.toString();
        } finally {
            PROGRESS.remove(scanId);
        }
    }

    /** Gray pairs (highest score first, up to limit) get a verdict; cached by model + both sides. */
    /** Spaces call starts so a model stays under its requests-per-minute quota. rpm <= 0: unpaced. */
    private static final class Pacer {
        private final long intervalMs;
        private long next;

        Pacer(int rpm) {
            intervalMs = rpm > 0 ? 60_000L / rpm : 0;
        }

        void await() throws InterruptedException {
            if (intervalMs == 0) {
                return;
            }
            long wait;
            synchronized (this) {
                long now = System.currentTimeMillis();
                next = Math.max(next, now);
                wait = next - now;
                next += intervalMs;
            }
            if (wait > 0) {
                Thread.sleep(wait);
            }
        }
    }

    private static int[] judge(Gemini g, String model, Analyzer.Analysis an, int limit, int rpm,
                               Map<String, JsonNode> cache, String scanId) throws Exception {
        List<Analyzer.Pair> gray = an.pairs.stream().filter(p -> "gray".equals(p.kind)).limit(limit).toList();
        int workers = model.contains("pro") ? 3 : 8; // Pro/preview models have tighter quotas
        Pacer pacer = new Pacer(rpm);
        java.util.concurrent.atomic.AtomicReference<String> halted = new java.util.concurrent.atomic.AtomicReference<>();
        AtomicInteger done = new AtomicInteger(), failed = new AtomicInteger();
        phase(scanId, "judging", 0, gray.size());
        ExecutorService pool = Executors.newFixedThreadPool(workers);
        try {
            List<Future<?>> fs = new ArrayList<>();
            for (Analyzer.Pair p : gray) {
                fs.add(pool.submit(() -> {
                    Cap a = an.caps.get(p.a), b = an.caps.get(p.b);
                    String sa = side(a), sb = side(b);
                    String key = Gemini.sha256(model + "\u0000" + (sa.compareTo(sb) <= 0 ? sa + "\u0000" + sb : sb + "\u0000" + sa));
                    JsonNode hit = cache.get(key);
                    if (hit != null) {
                        p.verdict = hit.path("verdict").asText(null);
                        p.rationale = hit.path("rationale").asText(null);
                    } else if (halted.get() != null) { // a 429 already hit: don't push more calls into the limit
                        p.rationale = "(not judged: stopped after " + halted.get() + ")";
                        failed.incrementAndGet();
                    } else {
                        try {
                            pacer.await();
                        } catch (InterruptedException ie) {
                            Thread.currentThread().interrupt();
                            return;
                        }
                        String prompt = String.format(PROMPT, a.type, a.asset, a.name, Http.trunc(a.description, 800),
                                b.type, b.asset, b.name, Http.trunc(b.description, 800));
                        for (int attempt = 0; attempt < 3; attempt++) {
                            try {
                                JsonNode v = g.generateJson(model, prompt);
                                p.verdict = v.path("verdict").asText(null);
                                p.rationale = v.path("rationale").asText(null);
                                ObjectNode c = Http.JSON.createObjectNode();
                                c.put("verdict", p.verdict).put("rationale", p.rationale);
                                synchronized (cache) {
                                    cache.put(key, c);
                                }
                                break;
                            } catch (Gemini.RateLimited e) { // never retry a 429; stop the rest
                                p.rationale = "(judge unavailable: " + e.getMessage() + ")";
                                halted.compareAndSet(null, e.getMessage());
                                failed.incrementAndGet();
                                break;
                            } catch (Exception e) {
                                if (attempt == 2) {
                                    p.rationale = "(judge unavailable: " + e.getMessage() + ")";
                                    failed.incrementAndGet();
                                } else {
                                    try {
                                        Thread.sleep(1000L << attempt);
                                    } catch (InterruptedException ie) {
                                        Thread.currentThread().interrupt();
                                        return;
                                    }
                                }
                            }
                        }
                    }
                    phase(scanId, "judging", done.incrementAndGet(), gray.size());
                }));
            }
            for (Future<?> f : fs) {
                f.get();
            }
        } finally {
            pool.shutdownNow();
        }
        return new int[]{(int) gray.stream().filter(p -> p.verdict != null).count(), failed.get()};
    }

    private static String side(Cap c) {
        return c.type + "\u0001" + c.asset + "\u0001" + c.name + "\u0001" + Http.trunc(c.description, 800);
    }

    private static ObjectNode render(String scanId, Analyzer.Analysis an, Exchange.Result ing, String embedModel,
                                     String judgeModel, double red, double gray, int[] judgeStats, long ms) {
        ObjectNode r = Http.JSON.createObjectNode();
        r.put("scanId", scanId).put("generatedAt", Instant.now().toString())
                .put("embeddingModel", "gemini:" + embedModel).put("judgeModel", judgeModel)
                .put("durationSeconds", Math.round(ms / 100.0) / 10.0);
        r.putObject("thresholds").put("redundant", red).put("gray", gray);

        ArrayNode caps = r.putArray("capabilities");
        Map<String, int[]> perAsset = new HashMap<>(); // assetKey -> {count, redundant, gray}
        for (int i = 0; i < an.caps.size(); i++) {
            caps.add(cap(an, i));
            int[] c = perAsset.computeIfAbsent(an.caps.get(i).assetKey, k -> new int[3]);
            c[0]++;
            if ("redundant".equals(an.status[i])) {
                c[1]++;
            } else if ("gray".equals(an.status[i])) {
                c[2]++;
            }
        }

        ArrayNode assets = r.putArray("assets");
        Set<String> listed = new HashSet<>();
        // Agents can appear twice: the Exchange agent stub and its card inside an agent network.
        // Keep one row per agent name: the one that carries the skills.
        Set<String> agentNamesWithSkills = new HashSet<>();
        for (Exchange.AssetInfo info : ing.assets) {
            for (Cap c : info.caps) {
                if ("agent".equals(c.type)) {
                    agentNamesWithSkills.add(c.asset.toLowerCase(java.util.Locale.ROOT));
                }
            }
        }
        Set<String> listedAgentNames = new HashSet<>();
        int undocumented = 0;
        for (Exchange.AssetInfo info : ing.assets) {
            if ("agent".equals(info.exchangeType) && info.caps.isEmpty()
                    && agentNamesWithSkills.contains(info.name.toLowerCase(java.util.Locale.ROOT))) {
                continue; // stub of an agent whose skills are listed from its network card
            }
            Map<String, Cap> byKey = new LinkedHashMap<>();
            for (Cap c : info.caps) {
                byKey.putIfAbsent(c.assetKey, c);
            }
            if (byKey.isEmpty()) {
                ObjectNode a = assetNode(info, info.assetId, info.name, info.type, new int[3]);
                a.put("gap", info.gap);
                undocumented += info.gap ? 1 : 0;
                assets.add(a);
                continue;
            }
            for (Cap c : byKey.values()) {
                if ("agent".equals(c.type) && !listedAgentNames.add(c.asset.toLowerCase(java.util.Locale.ROOT))) {
                    continue; // same agent already listed (stub with a live card + network card)
                }
                if (listed.add(c.assetKey)) {
                    assets.add(assetNode(info, c.assetKey, c.asset, c.type, perAsset.getOrDefault(c.assetKey, new int[3])));
                }
            }
        }

        int[] counts = new int[3]; // unique, gray, redundant
        for (String s : an.status) {
            counts["unique".equals(s) ? 0 : "gray".equals(s) ? 1 : 2]++;
        }
        long distinctAssets = an.caps.stream().map(c -> c.asset).distinct().count();
        ObjectNode cn = r.putObject("counts");
        cn.put("assets", distinctAssets).put("catalogAssets", assets.size()).put("capabilities", an.caps.size()).put("unique", counts[0])
                .put("gray", counts[1]).put("redundant", counts[2]).put("clusters", an.clusters.size())
                .put("undocumented", undocumented).put("grayJudged", judgeStats[0]).put("grayJudgeFailed", judgeStats[1]);

        ArrayNode recs = r.putArray("recommendations");
        an.recommendations.forEach(recs::add);
        ArrayNode ov = r.putArray("overlapMatrix");
        for (Analyzer.Overlap o : an.overlaps) {
            ov.addObject().put("source", o.source).put("target", o.target).put("redundant", o.redundant)
                    .put("gray", o.gray).put("total", o.total).put("coverage", Math.round(o.coverage() * 1000) / 1000.0)
                    .put("relationship", o.relationship);
        }
        ArrayNode cl = r.putArray("clusters");
        for (Analyzer.Cluster c : an.clusters) {
            ObjectNode node = cl.addObject();
            node.put("canonical", an.caps.get(c.canonical).id()).put("meanScore", Math.round(c.meanScore * 1000) / 1000.0);
            ArrayNode m = node.putArray("members");
            c.members.forEach(i -> m.add(cap(an, i)));
        }
        ArrayNode gp = r.putArray("grayPairs");
        for (Analyzer.Pair p : an.pairs) {
            if ("gray".equals(p.kind)) {
                ObjectNode node = gp.addObject();
                node.set("a", cap(an, p.a));
                node.set("b", cap(an, p.b));
                node.put("score", Math.round(p.score * 1000) / 1000.0).put("verdict", p.verdict).put("rationale", p.rationale);
            }
        }
        return r;
    }

    private static ObjectNode cap(Analyzer.Analysis an, int i) {
        Cap c = an.caps.get(i);
        ObjectNode n = Http.JSON.createObjectNode();
        n.put("id", c.id()).put("asset", c.asset).put("assetKey", c.assetKey).put("type", c.type)
                .put("name", c.name).put("description", c.description).put("status", an.status[i])
                .put("textKey", Gemini.sha256(c.text()));
        return n;
    }

    private static ObjectNode assetNode(Exchange.AssetInfo info, String key, String name, String type, int[] c) {
        ObjectNode a = Http.JSON.createObjectNode();
        a.put("assetId", key).put("groupId", info.groupId).put("name", name).put("exchangeType", info.exchangeType)
                .put("exchangeAssetId", info.assetId).put("type", type).put("version", info.version)
                .put("capabilityCount", c[0]).put("capabilitySource", info.capabilitySource).put("gap", false)
                .put("note", info.note).put("redundantCount", c[1]).put("grayCount", c[2]);
        return a;
    }

    /** "Does this already exist?" against a stored scan result. Returns a JSON array of matches. */
    public static String find(String configJson, String resultJson, String embedCacheJson, String description, int topK)
            throws Exception {
        JsonNode cfg = readTree(configJson), result = readTree(resultJson);
        Map<String, String> cache = readMap(embedCacheJson);
        JsonNode gem = cfg.path("gemini");
        Gemini g = new Gemini(gem.path("apiKey").asText());
        float[] q = g.embed(gem.path("embedModel").asText("gemini-embedding-001"), List.of(description), new HashMap<>())[0];
        double red = result.path("thresholds").path("redundant").asDouble(0.91);
        double gray = result.path("thresholds").path("gray").asDouble(0.88);
        List<double[]> scored = new ArrayList<>();
        JsonNode caps = result.path("capabilities");
        for (int i = 0; i < caps.size(); i++) {
            String v = cache.get(caps.get(i).path("textKey").asText());
            if (v == null) {
                continue;
            }
            float[] e = Gemini.normalize(Gemini.decode(v));
            double d = 0;
            for (int k = 0; k < e.length && k < q.length; k++) {
                d += e[k] * q[k];
            }
            scored.add(new double[]{i, d});
        }
        if (scored.isEmpty()) { // fail loudly: an empty answer here means broken inputs, not "nothing similar"
            throw new IllegalStateException("find: 0 of " + caps.size() + " capabilities have a cached embedding ("
                    + cache.size() + " cache entries). Re-run a scan.");
        }
        scored.sort((x, y) -> Double.compare(y[1], x[1]));
        ArrayNode out = Http.JSON.createArrayNode();
        for (double[] s : scored.subList(0, Math.min(Math.max(1, topK), scored.size()))) {
            ObjectNode m = out.addObject();
            ObjectNode c = (ObjectNode) caps.get((int) s[0]).deepCopy();
            c.remove("textKey");
            m.set("capability", c);
            m.put("score", Math.round(s[1] * 1000) / 1000.0)
                    .put("verdict", s[1] >= red ? "redundant" : s[1] >= gray ? "gray" : "likely-new");
        }
        return out.toString();
    }

    /** Parses JSON handed over from Mule. Values read back from Object Store can arrive
     *  double-encoded ("\"{...}\""); unwrap so they don't silently parse as an empty object. */
    static JsonNode readTree(String json) throws Exception {
        if (json == null || json.isBlank() || "null".equals(json.strip())) {
            return Http.JSON.createObjectNode();
        }
        JsonNode n = Http.json(json);
        for (int i = 0; i < 3 && n.isTextual(); i++) {
            n = Http.json(n.asText());
        }
        return n;
    }

    private static Map<String, String> readMap(String json) throws Exception {
        Map<String, String> m = new HashMap<>();
        readTree(json).fields().forEachRemaining(e -> m.put(e.getKey(), e.getValue().asText()));
        return m;
    }

    private SprawlEngine() {
    }
}
