package com.mycompany.sprawl;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;
import java.util.regex.Pattern;
import java.util.stream.Collectors;

/** Classifies capabilities as unique / gray / redundant, clusters redundant ones, rolls
 *  overlap up to asset level and writes consolidation recommendations. Port of analyze.py. */
final class Analyzer {
    static final class Pair {
        final int a, b;
        final double score;
        String kind; // redundant | gray
        String verdict, rationale;

        Pair(int a, int b, double score, String kind) {
            this.a = a;
            this.b = b;
            this.score = score;
            this.kind = kind;
        }
    }

    static final class Cluster {
        List<Integer> members;
        int canonical;
        double meanScore;
    }

    static final class Overlap {
        String source, target, relationship; // relationship: null = possible sprawl
        int redundant, gray, total;

        double coverage() {
            return total == 0 ? 0 : (double) redundant / total;
        }
    }

    static final class Analysis {
        List<Cap> caps;
        String[] status;
        List<Pair> pairs = new ArrayList<>();
        List<Cluster> clusters = new ArrayList<>();
        List<Overlap> overlaps = new ArrayList<>();
        List<String> recommendations = new ArrayList<>();
        double[][] sim;
    }

    static Analysis analyze(List<Cap> caps, float[][] vec, double redundant, double gray, Set<String> preferred) {
        int n = caps.size();
        double[][] sim = new double[n][n];
        for (int i = 0; i < n; i++) {
            for (int j = i; j < n; j++) {
                double d = 0;
                for (int k = 0; k < vec[i].length; k++) {
                    d += vec[i][k] * vec[j][k];
                }
                d = Math.max(-1, Math.min(1, d));
                sim[i][j] = d;
                sim[j][i] = d;
            }
        }
        Analysis an = new Analysis();
        an.caps = caps;
        an.sim = sim;
        Map<String, String> kinds = new HashMap<>();
        for (Cap c : caps) {
            kinds.putIfAbsent(c.asset, c.type);
        }
        boolean[] thin = new boolean[n];
        for (int i = 0; i < n; i++) {
            thin[i] = caps.get(i).thin();
        }

        for (int i = 0; i < n; i++) {
            for (int j = i + 1; j < n; j++) {
                if (caps.get(i).asset.equals(caps.get(j).asset)) {
                    continue; // only cross-asset pairs count as sprawl
                }
                if (relationship(caps.get(i).asset, caps.get(j).asset, kinds) != null) {
                    continue; // designed overlap (facade, agent calling its tools, API-led layers): not sprawl
                }
                double s = sim[i][j];
                if (s >= redundant && !(thin[i] || thin[j])) {
                    an.pairs.add(new Pair(i, j, s, "redundant"));
                } else if (s >= gray) {
                    an.pairs.add(new Pair(i, j, s, "gray"));
                }
            }
        }
        an.pairs.sort(Comparator.comparingDouble((Pair p) -> p.score).reversed());

        // Average-linkage merge: groups join only if their mean cross-similarity still clears
        // the redundant bar, so A~B~C doesn't snowball. A group never holds two capabilities
        // of one asset. A redundant pair that can't merge is downgraded to gray.
        int[] parent = new int[n];
        LinkedHashMap<Integer, List<Integer>> membersOf = new LinkedHashMap<>();
        for (int i = 0; i < n; i++) {
            parent[i] = i;
            membersOf.put(i, new ArrayList<>(List.of(i)));
        }
        for (Pair p : an.pairs) {
            if (!"redundant".equals(p.kind)) {
                continue;
            }
            int ra = find(parent, p.a), rb = find(parent, p.b);
            if (ra == rb) {
                continue;
            }
            Set<String> assetsA = membersOf.get(ra).stream().map(i -> caps.get(i).asset).collect(Collectors.toSet());
            if (membersOf.get(rb).stream().anyMatch(i -> assetsA.contains(caps.get(i).asset))) {
                p.kind = "gray";
                continue;
            }
            double sum = 0;
            for (int x : membersOf.get(ra)) {
                for (int y : membersOf.get(rb)) {
                    sum += sim[x][y];
                }
            }
            if (sum / (membersOf.get(ra).size() * membersOf.get(rb).size()) >= redundant) {
                parent[ra] = rb;
                List<Integer> merged = new ArrayList<>(membersOf.remove(ra));
                merged.addAll(membersOf.remove(rb));
                membersOf.put(rb, merged);
            } else {
                p.kind = "gray";
            }
        }

        for (List<Integer> members : membersOf.values()) {
            if (members.size() < 2) {
                continue;
            }
            int best = members.get(0);
            double bestScore = Double.NEGATIVE_INFINITY, off = 0;
            for (int x : members) {
                double rowSum = 0;
                for (int y : members) {
                    rowSum += sim[x][y];
                    if (x != y) {
                        off += sim[x][y];
                    }
                }
                // Preferred (e.g. gateway-governed) assets win; otherwise the medoid.
                double score = rowSum + (preferred.contains(caps.get(x).asset) ? members.size() : 0);
                if (score > bestScore) {
                    bestScore = score;
                    best = x;
                }
            }
            Cluster c = new Cluster();
            c.members = members.stream().sorted().collect(Collectors.toList());
            c.canonical = best;
            c.meanScore = off / (members.size() * (members.size() - 1.0));
            an.clusters.add(c);
        }
        an.clusters.sort(Comparator.comparingInt((Cluster c) -> c.members.size())
                .thenComparingDouble(c -> c.meanScore).reversed());

        an.status = new String[n];
        java.util.Arrays.fill(an.status, "unique");
        for (Pair p : an.pairs) {
            if ("gray".equals(p.kind)) {
                an.status[p.a] = "gray";
                an.status[p.b] = "gray";
            }
        }
        for (Cluster c : an.clusters) {
            for (int i : c.members) {
                an.status[i] = "redundant";
            }
        }

        // Thin capabilities can't count as redundant at asset level either.
        double[][] capped = new double[n][];
        for (int i = 0; i < n; i++) {
            capped[i] = sim[i].clone();
            for (int j = 0; j < n; j++) {
                if (thin[i] || thin[j]) {
                    capped[i][j] = Math.min(capped[i][j], redundant - 1e-6);
                }
            }
        }
        an.overlaps = overlaps(caps, capped, redundant, gray);
        for (Overlap o : an.overlaps) {
            o.relationship = relationship(o.source, o.target, kinds);
        }
        an.recommendations = recommendations(caps, an.clusters, an.overlaps, 0.5, 0.8, preferred);
        return an;
    }

    private static int find(int[] parent, int x) {
        while (parent[x] != x) {
            parent[x] = parent[parent[x]];
            x = parent[x];
        }
        return x;
    }

    static List<Overlap> overlaps(List<Cap> caps, double[][] sim, double redundant, double gray) {
        LinkedHashMap<String, List<Integer>> byAsset = new LinkedHashMap<>();
        for (int i = 0; i < caps.size(); i++) {
            byAsset.computeIfAbsent(caps.get(i).asset, k -> new ArrayList<>()).add(i);
        }
        List<Overlap> out = new ArrayList<>();
        for (Map.Entry<String, List<Integer>> s : byAsset.entrySet()) {
            for (Map.Entry<String, List<Integer>> t : byAsset.entrySet()) {
                if (s.getKey().equals(t.getKey())) {
                    continue;
                }
                int red = 0, gr = 0;
                for (int i : s.getValue()) {
                    double best = Double.NEGATIVE_INFINITY;
                    for (int j : t.getValue()) {
                        best = Math.max(best, sim[i][j]);
                    }
                    if (best >= redundant) {
                        red++;
                    } else if (best >= gray) {
                        gr++;
                    }
                }
                if (red > 0 || gr > 0) {
                    Overlap o = new Overlap();
                    o.source = s.getKey();
                    o.target = t.getKey();
                    o.redundant = red;
                    o.gray = gr;
                    o.total = s.getValue().size();
                    out.add(o);
                }
            }
        }
        out.sort(Comparator.comparingDouble(Overlap::coverage).thenComparingInt(o -> o.redundant).reversed());
        return out;
    }

    private static final Pattern LAYER = Pattern.compile("^(exp|prc|sys|proc|xapi|papi|sapi)[-_ ]", Pattern.CASE_INSENSITIVE);

    private static String stripLayer(String a) {
        return LAYER.matcher(a.toLowerCase(Locale.ROOT)).replaceFirst("");
    }

    /** Designed relationship between two assets, or null: facade (MCP/agent over an API),
     *  agent-tool (agent calling an MCP server) or api-led-layer (exp/prc/sys of one service). */
    static String relationship(String a, String b, Map<String, String> kind) {
        if (stripLayer(a).equals(stripLayer(b)) && (LAYER.matcher(a).find() || LAYER.matcher(b).find())) {
            return "api-led-layer";
        }
        Set<String> k = new HashSet<>(List.of(kind.get(a), kind.get(b)));
        if (k.equals(Set.of("agent", "mcp"))) {
            return "agent-tool";
        }
        if (k.equals(Set.of("mcp", "api")) || k.equals(Set.of("agent", "api"))) {
            return "facade";
        }
        return null;
    }

    /** Overlap that is expected by design: say so instead of recommending retirement. */
    static String intentional(String a, String b, Map<String, String> kind) {
        if (stripLayer(a).equals(stripLayer(b)) && (LAYER.matcher(a).find() || LAYER.matcher(b).find())) {
            return "'" + a + "' and '" + b + "' look like API-led layers of the same service; overlap is "
                    + "expected. Check that each layer adds value rather than passing straight through.";
        }
        Set<String> k = new HashSet<>(List.of(kind.get(a), kind.get(b))); // Set.of rejects duplicates
        if (k.equals(Set.of("agent", "mcp"))) {
            String agent = "agent".equals(kind.get(a)) ? a : b, server = agent.equals(a) ? b : a;
            return "'" + agent + "' (agent) has skills matching '" + server + "' (mcp) tools: likely the agent "
                    + "calling that server. Fine if so; sprawl if the agent re-implements the tools.";
        }
        if (k.equals(Set.of("mcp", "api")) || k.equals(Set.of("agent", "api"))) {
            String facade = "api".equals(kind.get(a)) ? b : a, api = facade.equals(a) ? b : a;
            return "'" + facade + "' (" + kind.get(facade) + ") mirrors '" + api + "' (api): likely a facade over it. "
                    + "Fine if it calls '" + api + "'; sprawl if it re-implements the backend call.";
        }
        return null;
    }

    static List<String> recommendations(List<Cap> caps, List<Cluster> clusters, List<Overlap> overlaps,
                                        double consolidateAt, double nearDuplicateAt, Set<String> preferred) {
        List<String> recs = new ArrayList<>();
        Map<String, Integer> size = new HashMap<>();
        Map<String, String> kind = new HashMap<>();
        for (Cap c : caps) {
            size.merge(c.asset, 1, Integer::sum);
            kind.putIfAbsent(c.asset, c.type);
        }
        Map<String, Overlap> cov = new HashMap<>();
        for (Overlap o : overlaps) {
            cov.put(o.source + "\u0000" + o.target, o);
        }
        Set<Set<String>> seen = new HashSet<>();
        Set<String> folded = new HashSet<>();
        Map<String, TreeSet<String>> families = new HashMap<>();
        // overlaps are sorted by coverage desc, so the first direction seen for a pair is the
        // more-covered asset: that's the one to fold into the other.
        for (Overlap o : overlaps) {
            Set<String> key = Set.of(o.source, o.target);
            if (o.coverage() < consolidateAt || seen.contains(key) || folded.contains(o.source)) {
                continue;
            }
            seen.add(key);
            Overlap back = cov.get(o.target + "\u0000" + o.source);
            String note = intentional(o.source, o.target, kind);
            if (note != null) {
                folded.add(o.source);
                recs.add(note);
            } else if (back != null && back.coverage() >= nearDuplicateAt && o.coverage() >= nearDuplicateAt) {
                TreeSet<String> group = new TreeSet<>(families.getOrDefault(o.source, new TreeSet<>(Set.of(o.source))));
                group.addAll(families.getOrDefault(o.target, new TreeSet<>(Set.of(o.target))));
                for (String a : group) {
                    families.put(a, group);
                }
            } else if (families.containsKey(o.source)) {
                // already covered by its near-duplicate family
            } else if (preferred.contains(o.source) && !preferred.contains(o.target)) {
                folded.add(o.source);
                recs.add("'" + o.target + "' duplicates " + o.redundant + "/" + o.total + " capabilities of preferred '"
                        + o.source + "'. Route those callers through '" + o.source + "'.");
            } else {
                folded.add(o.source);
                recs.add("Retire or fold '" + o.source + "' into '" + o.target + "': " + o.redundant + "/" + o.total
                        + " of its capabilities already exist there.");
            }
        }

        Set<Set<String>> groups = new LinkedHashSet<>(families.values());
        List<String> familyRecs = new ArrayList<>();
        List<Integer> familySizes = new ArrayList<>();
        for (Set<String> g : groups) {
            String keep = g.stream().min(Comparator.comparing((String a) -> !preferred.contains(a))
                    .thenComparing(a -> -size.getOrDefault(a, 0)).thenComparing(a -> a)).orElseThrow();
            String others = g.stream().filter(a -> !a.equals(keep)).sorted().map(a -> "'" + a + "'")
                    .collect(Collectors.joining(", "));
            familyRecs.add(g.size() + " near-duplicate assets: '" + keep + "' and " + others
                    + " expose the same capabilities. Consolidate into '" + keep + "'.");
            familySizes.add(g.size());
        }
        List<Integer> order = new ArrayList<>();
        for (int i = 0; i < familyRecs.size(); i++) {
            order.add(i);
        }
        order.sort(Comparator.comparingInt(familySizes::get).reversed());
        List<String> out = new ArrayList<>();
        for (int i : order) {
            out.add(familyRecs.get(i));
        }
        out.addAll(recs);

        for (Cluster c : clusters) {
            Set<String> assets = new TreeSet<>();
            for (int i : c.members) {
                assets.add(caps.get(i).asset);
            }
            if (assets.size() < 3) {
                continue;
            }
            if (assets.stream().map(Analyzer::stripLayer).distinct().count() == 1) {
                continue; // API-led layers of one service: reported above as expected
            }
            if (groups.stream().anyMatch(g -> g.containsAll(assets))) {
                continue; // already reported as a near-duplicate family
            }
            Cap canon = caps.get(c.canonical);
            out.add("'" + canon.name + "' is implemented " + c.members.size() + " times across " + assets.size()
                    + " assets (" + String.join(", ", assets) + "). Standardise on '" + canon.asset + "' and reuse it.");
        }
        return out;
    }

    private Analyzer() {
    }
}
