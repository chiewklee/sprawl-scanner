package com.mycompany.sprawl;

import com.fasterxml.jackson.databind.JsonNode;

import java.net.http.HttpClient;
import java.net.http.HttpResponse;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Base64;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Gemini REST: batch embeddings and JSON-mode generation (the judge). */
final class Gemini {
    private static final String BASE = "https://generativelanguage.googleapis.com/v1beta/models/";
    private static final int BATCH = 100;

    static final class RateLimited extends Exception {
        RateLimited(String m) {
            super(m);
        }
    }

    private final String apiKey;
    private final HttpClient http = Http.client(120);

    Gemini(String apiKey) {
        if (apiKey == null || apiKey.isBlank()) {
            throw new IllegalArgumentException("gemini.apiKey is not set");
        }
        this.apiKey = apiKey;
    }

    static String sha256(String s) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(s.getBytes(StandardCharsets.UTF_8)));
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    static String encode(float[] v) {
        ByteBuffer b = ByteBuffer.allocate(v.length * 4).order(ByteOrder.LITTLE_ENDIAN);
        for (float f : v) {
            b.putFloat(f);
        }
        return Base64.getEncoder().encodeToString(b.array());
    }

    static float[] decode(String s) {
        ByteBuffer b = ByteBuffer.wrap(Base64.getDecoder().decode(s)).order(ByteOrder.LITTLE_ENDIAN);
        float[] v = new float[b.remaining() / 4];
        for (int i = 0; i < v.length; i++) {
            v[i] = b.getFloat();
        }
        return v;
    }

    static float[] normalize(float[] v) {
        double n = 0;
        for (float f : v) {
            n += (double) f * f;
        }
        n = Math.sqrt(n);
        if (n == 0) {
            return v;
        }
        float[] o = new float[v.length];
        for (int i = 0; i < v.length; i++) {
            o[i] = (float) (v[i] / n);
        }
        return o;
    }

    /** Embeds texts not already in cache (keyed by sha256(text)); cache is updated in place.
     *  Returns L2-normalised vectors in input order. */
    float[][] embed(String model, List<String> texts, Map<String, String> cache) throws Exception {
        Map<String, String> missing = new LinkedHashMap<>();
        for (String t : texts) {
            String k = sha256(t);
            if (!cache.containsKey(k)) {
                missing.putIfAbsent(k, t);
            }
        }
        List<Map.Entry<String, String>> todo = new ArrayList<>(missing.entrySet());
        for (int i = 0; i < todo.size(); i += BATCH) {
            List<Map.Entry<String, String>> batch = todo.subList(i, Math.min(todo.size(), i + BATCH));
            List<Map<String, Object>> reqs = new ArrayList<>();
            for (Map.Entry<String, String> e : batch) {
                // One request per text: gemini-embedding-2 merges a multi-part content into one vector.
                reqs.add(Map.of("model", "models/" + model, "taskType", "SEMANTIC_SIMILARITY",
                        "content", Map.of("parts", List.of(Map.of("text", e.getValue())))));
            }
            HttpResponse<String> r = Http.postJson(http, BASE + model + ":batchEmbedContents",
                    Map.of("x-goog-api-key", apiKey), Map.of("requests", reqs), 120);
            if (r.statusCode() != 200) {
                throw new IllegalStateException("embed HTTP " + r.statusCode() + ": " + Http.trunc(r.body(), 300));
            }
            JsonNode embs = Http.json(r.body()).path("embeddings");
            if (embs.size() != batch.size()) {
                throw new IllegalStateException(model + " returned " + embs.size() + " embeddings for " + batch.size() + " texts");
            }
            for (int j = 0; j < batch.size(); j++) {
                JsonNode values = embs.get(j).path("values");
                float[] v = new float[values.size()];
                for (int d = 0; d < v.length; d++) {
                    v[d] = (float) values.get(d).asDouble();
                }
                cache.put(batch.get(j).getKey(), encode(v));
            }
        }
        float[][] out = new float[texts.size()][];
        for (int i = 0; i < texts.size(); i++) {
            out[i] = normalize(decode(cache.get(sha256(texts.get(i)))));
        }
        return out;
    }

    /** One JSON-mode generation call. Throws RateLimited on HTTP 429 (never retried). */
    JsonNode generateJson(String model, String prompt) throws Exception {
        Map<String, Object> body = Map.of(
                "contents", List.of(Map.of("parts", List.of(Map.of("text", prompt)))),
                "generationConfig", Map.of("responseMimeType", "application/json", "temperature", 0));
        HttpResponse<String> r = Http.postJson(http, BASE + model + ":generateContent",
                Map.of("x-goog-api-key", apiKey), body, 120);
        if (r.statusCode() == 429) {
            String quota = "";
            try {
                for (JsonNode d : Http.json(r.body()).path("error").path("details")) {
                    for (JsonNode v : d.path("violations")) {
                        quota = v.path("quotaId").asText(quota);
                    }
                }
            } catch (Exception ignored) {
                // body isn't JSON: keep the bare status
            }
            throw new RateLimited(quota.isEmpty() ? "HTTP 429" : "HTTP 429 " + quota);
        }
        if (r.statusCode() != 200) {
            throw new IllegalStateException("HTTP " + r.statusCode());
        }
        JsonNode text = Http.json(r.body()).path("candidates").path(0).path("content").path("parts").path(0).path("text");
        return Http.json(text.asText());
    }
}
