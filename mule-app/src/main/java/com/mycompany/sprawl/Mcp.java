package com.mycompany.sprawl;

import com.fasterxml.jackson.databind.JsonNode;

import java.net.http.HttpClient;
import java.net.http.HttpResponse;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/** Minimal MCP Streamable HTTP client: initialize -> notifications/initialized -> tools/list. */
final class Mcp {
    static final String PROTOCOL_VERSION = "2025-06-18";

    record Tool(String name, String description) {
    }

    static List<Tool> toolsList(String url, Map<String, String> headers, int timeout) throws Exception {
        HttpClient c = Http.client(timeout);
        Map<String, String> h = new HashMap<>(headers);
        h.put("Accept", "application/json, text/event-stream");

        HttpResponse<String> init = Http.postJson(c, url, h, Map.of(
                "jsonrpc", "2.0", "id", 1, "method", "initialize",
                "params", Map.of("protocolVersion", PROTOCOL_VERSION, "capabilities", Map.of(),
                        "clientInfo", Map.of("name", "sprawl-scanner", "version", "1.0.0"))), timeout);
        if (init.statusCode() >= 400) {
            throw new IllegalStateException("initialize HTTP " + init.statusCode() + ": " + Http.trunc(init.body(), 200));
        }
        parse(init);
        init.headers().firstValue("mcp-session-id").ifPresent(sid -> h.put("mcp-session-id", sid));
        Http.postJson(c, url, h, Map.of("jsonrpc", "2.0", "method", "notifications/initialized"), timeout);

        List<Tool> tools = new ArrayList<>();
        String cursor = null;
        int id = 2;
        do {
            Map<String, Object> params = cursor == null ? Map.of() : Map.of("cursor", cursor);
            HttpResponse<String> r = Http.postJson(c, url, h, Map.of(
                    "jsonrpc", "2.0", "id", id++, "method", "tools/list", "params", params), timeout);
            if (r.statusCode() >= 400) {
                throw new IllegalStateException("tools/list HTTP " + r.statusCode() + ": " + Http.trunc(r.body(), 200));
            }
            JsonNode msg = parse(r);
            if (msg.has("error")) {
                throw new IllegalStateException("tools/list error: " + msg.get("error"));
            }
            for (JsonNode t : msg.path("result").path("tools")) {
                tools.add(new Tool(t.path("name").asText(), t.path("description").asText("")));
            }
            JsonNode next = msg.path("result").path("nextCursor");
            cursor = next.isTextual() && !next.asText().isEmpty() ? next.asText() : null;
        } while (cursor != null);
        return tools;
    }

    /** Streamable HTTP servers answer with plain JSON or an SSE stream. */
    static JsonNode parse(HttpResponse<String> r) throws Exception {
        String ct = r.headers().firstValue("content-type").orElse("");
        if (ct.contains("text/event-stream")) {
            for (String line : r.body().split("\\R")) {
                if (line.startsWith("data:")) {
                    JsonNode msg = Http.json(line.substring(5).strip());
                    if (msg.has("result") || msg.has("error")) {
                        return msg;
                    }
                }
            }
            throw new IllegalStateException("no JSON-RPC response in SSE stream");
        }
        return Http.json(r.body());
    }

    private Mcp() {
    }
}
