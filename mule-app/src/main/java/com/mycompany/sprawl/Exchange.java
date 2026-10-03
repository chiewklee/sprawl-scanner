package com.mycompany.sprawl;

import com.fasterxml.jackson.databind.JsonNode;
import org.yaml.snakeyaml.LoaderOptions;
import org.yaml.snakeyaml.Yaml;
import org.yaml.snakeyaml.constructor.AbstractConstruct;
import org.yaml.snakeyaml.constructor.SafeConstructor;
import org.yaml.snakeyaml.nodes.Node;

import java.io.ByteArrayInputStream;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.IntConsumer;
import java.util.zip.ZipEntry;
import java.util.zip.ZipInputStream;

/** Lists an Anypoint org's Exchange assets and turns each into capabilities. */
final class Exchange {
    static final List<String> DEFAULT_TYPES = List.of("rest-api", "http-api", "mcp", "agent", "agent-network");
    // Prefer the richest spec when an asset publishes several.
    static final List<String> CLASSIFIER_ORDER = List.of("fat-oas", "oas", "fat-raml", "raml",
            "mcp-metadata", "original-mcp-metadata", "fat-mcp-metadata",
            "agent-network", "original-agent-network", "agent-metadata", "original-agent-metadata");
    static final List<String> PACKAGING_ORDER = List.of("json", "yaml", "yml", "raml", "zip");
    static final List<String> HTTP_METHODS = List.of("get", "post", "put", "patch", "delete");

    /** Per-Exchange-asset outcome, for the Assets page. */
    static final class AssetInfo {
        String assetId, groupId, name, exchangeType, type, version, capabilitySource = "none", note;
        boolean gap;
        final List<Cap> caps = new ArrayList<>();
    }

    static final class Result {
        final List<AssetInfo> assets = new ArrayList<>();
        final List<Cap> caps = new ArrayList<>();
    }

    /** OAuth client-credentials rule for MCP servers whose upstream host matches. */
    static final class AuthRule {
        final String name, tokenUrl, clientId, clientSecret;
        final List<String> hosts;
        private volatile String token;
        private volatile long expiresAt;

        AuthRule(String name, List<String> hosts, String tokenUrl, String clientId, String clientSecret) {
            this.name = name;
            this.hosts = hosts;
            this.tokenUrl = tokenUrl;
            this.clientId = clientId;
            this.clientSecret = clientSecret;
        }

        boolean matches(String host) {
            return hosts.stream().anyMatch(h -> host.equalsIgnoreCase(h) || host.toLowerCase(Locale.ROOT).endsWith("." + h.toLowerCase(Locale.ROOT)));
        }

        synchronized String token(HttpClient http) throws Exception {
            if (token != null && System.currentTimeMillis() < expiresAt) {
                return token;
            }
            HttpResponse<String> r = Http.postForm(http, tokenUrl, Map.of("grant_type", "client_credentials",
                    "client_id", clientId, "client_secret", clientSecret), 30);
            if (r.statusCode() != 200) {
                throw new IllegalStateException(name + " token HTTP " + r.statusCode());
            }
            JsonNode j = Http.json(r.body());
            token = j.path("access_token").asText();
            expiresAt = System.currentTimeMillis() + Math.max(60, j.path("expires_in").asLong(3600) - 60) * 1000L;
            return token;
        }
    }

    private final String base, clientId, clientSecret;
    private final HttpClient http = Http.client(60);
    private final List<AuthRule> authRules = new ArrayList<>();
    private String token, org;

    void addAuthRule(AuthRule r) {
        authRules.add(r);
    }

    Exchange(String base, String clientId, String clientSecret, String orgId) {
        this.base = base.replaceAll("/+$", "");
        this.clientId = clientId;
        this.clientSecret = clientSecret;
        this.org = orgId == null || orgId.isBlank() ? null : orgId;
    }

    private Map<String, String> auth() {
        return Map.of("Authorization", "Bearer " + token);
    }

    void login() throws Exception {
        HttpResponse<String> r = Http.postJson(http, base + "/accounts/api/v2/oauth2/token", Map.of(),
                Map.of("grant_type", "client_credentials", "client_id", clientId, "client_secret", clientSecret), 30);
        if (r.statusCode() != 200) {
            throw new IllegalStateException("Anypoint token HTTP " + r.statusCode() + ": " + Http.trunc(r.body(), 200));
        }
        token = Http.json(r.body()).path("access_token").asText();
        if (org == null) {
            JsonNode me = Http.json(Http.get(http, base + "/accounts/api/me", auth(), 30).body());
            org = me.has("user") ? me.path("user").path("organization").path("id").asText()
                    : me.path("client").path("org_id").asText();
        }
    }

    Result scan(List<String> types, boolean liveMcp, int workers, IntConsumer onTotal, Runnable onAssetDone)
            throws Exception {
        login();
        Map<String, JsonNode> latest = new LinkedHashMap<>(); // one entry per asset; later versions win
        for (String t : types == null || types.isEmpty() ? DEFAULT_TYPES : types) {
            for (int offset = 0; ; offset += 100) {
                String q = Http.query(Map.of("organizationId", org, "types", t, "limit", 100, "offset", offset));
                HttpResponse<byte[]> r = Http.get(http, base + "/exchange/api/v2/assets/search?" + q, auth(), 60);
                if (r.statusCode() == 400) {
                    break; // unknown type in this region/version
                }
                if (r.statusCode() != 200) {
                    throw new IllegalStateException("Exchange search HTTP " + r.statusCode());
                }
                JsonNode page = Http.json(r.body());
                page.forEach(a -> latest.put(a.path("groupId").asText() + "/" + a.path("assetId").asText(), a));
                if (page.size() < 100) {
                    break;
                }
            }
        }
        onTotal.accept(latest.size());

        ExecutorService pool = Executors.newFixedThreadPool(Math.max(1, workers));
        try {
            List<Future<AssetInfo>> futures = new ArrayList<>();
            for (JsonNode a : latest.values()) {
                futures.add(pool.submit(() -> {
                    try {
                        return process(a, liveMcp);
                    } finally {
                        onAssetDone.run();
                    }
                }));
            }
            Result res = new Result();
            Map<String, Cap> unique = new LinkedHashMap<>(); // one agent card can sit in several networks
            for (Future<AssetInfo> f : futures) {
                AssetInfo info = f.get();
                res.assets.add(info);
                for (Cap c : info.caps) {
                    unique.put(c.asset + "\u0000" + c.name, c);
                }
            }
            res.caps.addAll(unique.values());
            return res;
        } finally {
            pool.shutdownNow();
        }
    }

    private AssetInfo process(JsonNode a, boolean liveMcp) {
        AssetInfo info = new AssetInfo();
        info.assetId = a.path("assetId").asText();
        info.groupId = a.path("groupId").asText();
        info.name = a.path("name").asText(info.assetId);
        info.exchangeType = a.path("type").asText();
        info.type = switch (info.exchangeType) {
            case "mcp" -> "mcp";
            case "agent", "agent-network" -> "agent";
            default -> "api";
        };
        info.version = a.path("version").asText();
        String src = "exchange:" + info.groupId + "/" + info.assetId + "/" + info.version;
        JsonNode detail = null;
        String liveReason = "";
        try {
            detail = Http.json(Http.get(http, base + "/exchange/api/v2/assets/" + info.groupId + "/"
                    + info.assetId + "/" + info.version, auth(), 60).body());
            fromFiles(info, detail, src);
        } catch (Exception e) {
            info.note = "spec download failed: " + e.getClass().getSimpleName();
        }
        if (info.caps.isEmpty() && "mcp".equals(info.type) && liveMcp && detail != null) {
            String r = liveTools(info, detail, src);
            if (!info.caps.isEmpty()) {
                info.capabilitySource = "live-tools-list";
                info.note = "no tool metadata in Exchange; read tools/list live from " + r;
            } else {
                liveReason = r;
            }
        }
        if (info.caps.isEmpty()) {
            String desc = a.path("description").asText("").strip();
            if (!desc.isEmpty()) {
                info.caps.add(new Cap(info.name, info.assetId, info.type, info.name, desc, src));
                info.capabilitySource = "description";
                info.note = "no parsable spec; used asset description";
            } else if ("agent".equals(info.exchangeType)) {
                info.note = "agent stub: skills come from its agent network";
            } else {
                info.gap = true;
                info.note = "no spec and no description" + (liveReason.isEmpty() ? "" : " (live: " + liveReason + ")");
            }
        }
        return info;
    }

    private void fromFiles(AssetInfo info, JsonNode detail, String src) throws Exception {
        List<JsonNode> files = new ArrayList<>();
        for (JsonNode f : detail.path("files")) {
            if (CLASSIFIER_ORDER.contains(f.path("classifier").asText()) && !link(f).isEmpty()) {
                files.add(f);
            }
        }
        files.sort((x, y) -> {
            int c = Integer.compare(CLASSIFIER_ORDER.indexOf(x.path("classifier").asText()),
                    CLASSIFIER_ORDER.indexOf(y.path("classifier").asText()));
            return c != 0 ? c : Integer.compare(rank(PACKAGING_ORDER, x.path("packaging").asText()),
                    rank(PACKAGING_ORDER, y.path("packaging").asText()));
        });
        for (JsonNode f : files) {
            String url = link(f);
            // presigned storage links reject extra auth headers; Anypoint URLs need them
            Map<String, String> h = URI.create(url).getHost().equals(URI.create(base).getHost()) ? auth() : Map.of();
            HttpResponse<byte[]> blob = Http.get(http, url, h, 60);
            if (blob.statusCode() != 200) {
                continue;
            }
            String text = mainText(blob.body(), f.path("packaging").asText(), f.path("mainFile").asText(null));
            if (parseDocument(text, info, src)) {
                return;
            }
        }
    }

    private static String link(JsonNode f) {
        String l = f.path("externalLink").asText("");
        return l.isEmpty() ? f.path("downloadURL").asText("") : l;
    }

    private static int rank(List<String> order, String v) {
        int i = order.indexOf(v);
        return i < 0 ? order.size() : i;
    }

    private String liveTools(AssetInfo info, JsonNode detail, String src) {
        String orgId = detail.path("organizationId").asText(detail.path("groupId").asText());
        List<String> reasons = new ArrayList<>();
        for (JsonNode inst : detail.path("instances")) {
            String env = inst.path("environmentId").asText(""), apiId = inst.path("id").asText("");
            if (env.isEmpty() || apiId.isEmpty()) {
                continue;
            }
            try {
                HttpResponse<byte[]> r = Http.get(http, base + "/apimanager/api/v1/organizations/" + orgId
                        + "/environments/" + env + "/apis/" + apiId, auth(), 30);
                if (r.statusCode() != 200) {
                    reasons.add("API Manager HTTP " + r.statusCode());
                    continue;
                }
                String upstream = Http.json(r.body()).path("endpoint").path("uri").asText("").replaceAll("/+$", "");
                if (upstream.isEmpty()) {
                    reasons.add("no upstream URL");
                    continue;
                }
                Map<String, String> headers = new LinkedHashMap<>();
                String host = URI.create(upstream).getHost();
                for (AuthRule rule : authRules) {
                    if (host != null && rule.matches(host)) {
                        headers.put("Authorization", "Bearer " + rule.token(http));
                        break;
                    }
                }
                // Most servers expose <upstream>/mcp; some (Salesforce platform MCP) are the upstream itself.
                List<String> candidates = upstream.endsWith("/mcp") ? List.of(upstream) : List.of(upstream + "/mcp", upstream);
                for (String url : candidates) {
                    try {
                        List<Mcp.Tool> tools = Mcp.toolsList(url, headers, 15);
                        for (Mcp.Tool t : tools) {
                            info.caps.add(new Cap(info.name, info.assetId, "mcp", t.name(), t.description(), url));
                        }
                        if (!tools.isEmpty()) {
                            return url;
                        }
                        reasons.add("server lists no tools");
                        break;
                    } catch (Exception e) {
                        String m = String.valueOf(e.getMessage());
                        if (!m.contains("HTTP 404") || url.equals(candidates.get(candidates.size() - 1))) {
                            throw e;
                        }
                    }
                }
            } catch (Exception e) {
                reasons.add("tools/list failed: " + Http.trunc(String.valueOf(e.getMessage()), 80));
            }
        }
        return reasons.isEmpty() ? "no running instance" : String.join("; ", reasons);
    }

    // ------------------------------------------------------------------ parsing

    static String mainText(byte[] blob, String packaging, String mainFile) throws Exception {
        boolean zip = "zip".equals(packaging) || (blob.length > 1 && blob[0] == 'P' && blob[1] == 'K');
        if (!zip) {
            return new String(blob, StandardCharsets.UTF_8);
        }
        Map<String, byte[]> entries = new LinkedHashMap<>();
        try (ZipInputStream z = new ZipInputStream(new ByteArrayInputStream(blob))) {
            for (ZipEntry e; (e = z.getNextEntry()) != null; ) {
                if (!e.isDirectory()) {
                    entries.put(e.getName(), z.readAllBytes());
                }
            }
        }
        // Exchange often leaves mainFile empty; the archive's exchange.json names the root spec.
        if ((mainFile == null || !entries.containsKey(mainFile)) && entries.containsKey("exchange.json")) {
            mainFile = Http.json(entries.get("exchange.json")).path("main").asText(null);
        }
        String name = mainFile != null && entries.containsKey(mainFile) ? mainFile : null;
        if (name == null) {
            for (String n : entries.keySet()) {
                if (n.matches(".*\\.(yaml|yml|json|raml)$") && !n.equals("exchange.json")
                        && !n.contains("exchange_modules") && !n.contains("/")) {
                    name = n;
                    break;
                }
            }
        }
        if (name == null) {
            throw new IllegalStateException("no spec file in archive");
        }
        return new String(entries.get(name), StandardCharsets.UTF_8);
    }

    /** RAML is YAML plus custom tags (!include etc.): keep the structure, drop the tags. */
    private static final class Lenient extends SafeConstructor {
        Lenient(LoaderOptions o) {
            super(o);
            AbstractConstruct drop = new AbstractConstruct() {
                @Override
                public Object construct(Node node) {
                    return null;
                }
            };
            this.yamlMultiConstructors.put("!", drop);
        }
    }

    static Object loadYaml(String text) {
        LoaderOptions o = new LoaderOptions();
        o.setCodePointLimit(64 * 1024 * 1024);
        o.setAllowDuplicateKeys(true);
        return new Yaml(new Lenient(o)).load(text);
    }

    @SuppressWarnings("unchecked")
    static Object load(String text) throws Exception {
        String t = text.strip();
        if (t.startsWith("{") || t.startsWith("[")) {
            return Http.JSON.readValue(t, Object.class);
        }
        return loadYaml(text);
    }

    /** Detect the spec flavour from content, not the classifier. Returns true if caps were found. */
    @SuppressWarnings("unchecked")
    static boolean parseDocument(String text, AssetInfo info, String src) throws Exception {
        int before = info.caps.size();
        if (text.stripLeading().startsWith("#%RAML")) {
            Object doc = loadYaml(text);
            if (doc instanceof Map<?, ?> m) {
                walkRaml((Map<Object, Object>) m, "", info, src);
            }
            info.capabilitySource = "raml";
            return info.caps.size() > before;
        }
        Object docObj = load(text);
        if (!(docObj instanceof Map<?, ?> raw)) {
            return false;
        }
        Map<String, Object> doc = (Map<String, Object>) raw;
        if (doc.containsKey("openapi") || doc.containsKey("swagger")) {
            Object paths = doc.get("paths");
            if (paths instanceof Map<?, ?> pm) {
                for (Map.Entry<?, ?> e : pm.entrySet()) {
                    if (!(e.getValue() instanceof Map<?, ?> item)) {
                        continue;
                    }
                    for (String m : HTTP_METHODS) {
                        if (item.get(m) instanceof Map<?, ?> op) {
                            String route = String.valueOf(e.getKey());
                            String desc = join(op.get("summary"), op.get("description"));
                            info.caps.add(new Cap(info.name, info.assetId, "api",
                                    Cap.operationName(str(op.get("operationId")), m, route),
                                    desc.isEmpty() ? m.toUpperCase(Locale.ROOT) + " " + route : desc, src));
                        }
                    }
                }
            }
            info.capabilitySource = "oas";
        } else if (doc.get("tools") instanceof List<?> tools) { // Exchange MCP asset descriptor
            for (Object t : tools) {
                if (t instanceof Map<?, ?> tm && tm.get("name") != null) {
                    info.caps.add(new Cap(info.name, info.assetId, "mcp", str(tm.get("name")), str(tm.get("description")), src));
                }
            }
            info.capabilitySource = "mcp-metadata";
        } else if (doc.get("skills") instanceof List<?>) { // A2A agent card
            skills(doc, info.name, info.assetId, info, src);
            info.capabilitySource = "agent-network";
        } else { // agent network: every card under brokers/agents is its own agent asset
            for (Map<String, Object> card : cards(doc)) {
                String cardName = card.get("name") == null ? info.name : str(card.get("name"));
                skills(card, cardName, info.assetId + ":" + slug(cardName), info, src);
            }
            info.capabilitySource = "agent-network";
        }
        return info.caps.size() > before;
    }

    @SuppressWarnings("unchecked")
    private static void walkRaml(Map<Object, Object> node, String prefix, AssetInfo info, String src) {
        for (Map.Entry<Object, Object> e : node.entrySet()) {
            if (!(e.getKey() instanceof String key) || !key.startsWith("/") || !(e.getValue() instanceof Map<?, ?> child)) {
                continue;
            }
            String path = prefix + key;
            for (String m : HTTP_METHODS) {
                if (child.containsKey(m)) {
                    Map<?, ?> op = child.get(m) instanceof Map<?, ?> om ? om : Map.of();
                    String desc = join(op.get("displayName"), op.get("description"));
                    String name = m.toUpperCase(Locale.ROOT) + " " + path;
                    info.caps.add(new Cap(info.name, info.assetId, "api", name, desc.isEmpty() ? name : desc, src));
                }
            }
            walkRaml((Map<Object, Object>) child, path, info, src);
        }
    }

    private static void skills(Map<?, ?> card, String asset, String key, AssetInfo info, String src) {
        if (!(card.get("skills") instanceof List<?> list)) {
            return;
        }
        for (Object o : list) {
            if (!(o instanceof Map<?, ?> s)) {
                continue;
            }
            String desc = str(s.get("description"));
            if (s.get("examples") instanceof List<?> ex && !ex.isEmpty()) {
                List<String> first = new ArrayList<>();
                for (Object x : ex.subList(0, Math.min(5, ex.size()))) {
                    first.add(String.valueOf(x));
                }
                desc += " Examples: " + String.join("; ", first);
            }
            String id = s.get("id") != null ? str(s.get("id")) : str(s.get("name"));
            info.caps.add(new Cap(asset, key, "agent", id, desc, src));
        }
    }

    /** All A2A cards (maps under a 'card' key that list skills) anywhere in the doc. */
    @SuppressWarnings("unchecked")
    private static List<Map<String, Object>> cards(Object node) {
        List<Map<String, Object>> found = new ArrayList<>();
        if (node instanceof Map<?, ?> m) {
            if (m.get("card") instanceof Map<?, ?> card && card.get("skills") instanceof List<?>) {
                found.add((Map<String, Object>) card);
            }
            for (Object v : m.values()) {
                found.addAll(cards(v));
            }
        } else if (node instanceof Collection<?> c) {
            for (Object v : c) {
                found.addAll(cards(v));
            }
        }
        return found;
    }

    static String slug(String s) {
        return s.toLowerCase(Locale.ROOT).replaceAll("[^a-z0-9]+", "-").replaceAll("(^-|-$)", "");
    }

    private static String str(Object o) {
        return o == null ? "" : String.valueOf(o).strip();
    }

    private static String join(Object a, Object b) {
        String x = str(a), y = str(b);
        return (x + " " + y).strip();
    }

    String orgId() {
        return org;
    }
}
