package com.mycompany.sprawl;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.io.IOException;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.Map;
import java.util.StringJoiner;

/** Thin wrapper over java.net.http with JSON helpers. */
final class Http {
    static final ObjectMapper JSON = new ObjectMapper();

    static HttpClient client(int timeoutSeconds) {
        // HTTP/1.1 + one client per logical session keeps MCP calls on one connection,
        // which pins them to one replica (servers without sticky sessions reject otherwise).
        return HttpClient.newBuilder()
                .version(HttpClient.Version.HTTP_1_1)
                .followRedirects(HttpClient.Redirect.NORMAL)
                .connectTimeout(Duration.ofSeconds(Math.min(timeoutSeconds, 20)))
                .build();
    }

    static String query(Map<String, ?> params) {
        StringJoiner j = new StringJoiner("&");
        params.forEach((k, v) -> j.add(URLEncoder.encode(k, StandardCharsets.UTF_8) + "="
                + URLEncoder.encode(String.valueOf(v), StandardCharsets.UTF_8)));
        return j.toString();
    }

    static HttpResponse<byte[]> get(HttpClient c, String url, Map<String, String> headers, int timeout)
            throws IOException, InterruptedException {
        HttpRequest.Builder b = HttpRequest.newBuilder(URI.create(url)).timeout(Duration.ofSeconds(timeout)).GET();
        headers.forEach(b::header);
        return c.send(b.build(), HttpResponse.BodyHandlers.ofByteArray());
    }

    static HttpResponse<String> postJson(HttpClient c, String url, Map<String, String> headers, Object body,
                                         int timeout) throws IOException, InterruptedException {
        HttpRequest.Builder b = HttpRequest.newBuilder(URI.create(url)).timeout(Duration.ofSeconds(timeout))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(JSON.writeValueAsString(body)));
        headers.forEach(b::header);
        return c.send(b.build(), HttpResponse.BodyHandlers.ofString());
    }

    static HttpResponse<String> postForm(HttpClient c, String url, Map<String, ?> form, int timeout)
            throws IOException, InterruptedException {
        HttpRequest r = HttpRequest.newBuilder(URI.create(url)).timeout(Duration.ofSeconds(timeout))
                .header("Content-Type", "application/x-www-form-urlencoded")
                .POST(HttpRequest.BodyPublishers.ofString(query(form))).build();
        return c.send(r, HttpResponse.BodyHandlers.ofString());
    }

    static JsonNode json(byte[] body) throws IOException {
        return JSON.readTree(body);
    }

    static JsonNode json(String body) throws IOException {
        return JSON.readTree(body);
    }

    static String trunc(String s, int n) {
        return s == null ? "" : s.length() <= n ? s : s.substring(0, n);
    }

    private Http() {
    }
}
