// Java 17+ and Jackson Databind 2.x. Run with the Maven project beside this file.
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.net.URI;
import java.net.http.*;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Duration;
import java.util.*;

public class Client {
    static final ObjectMapper JSON = new ObjectMapper();
    static final String BASE = System.getenv().getOrDefault("TRANSLATION_API", "https://translate.bugg.icu/v1").replaceAll("/$", "");
    static final HttpClient HTTP = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5))
        .followRedirects(HttpClient.Redirect.NEVER).build();

    static JsonNode call(String path, Object body, Map<String,String> headers) throws Exception {
        var builder = HttpRequest.newBuilder(URI.create(BASE + "/" + path))
            .timeout(Duration.ofSeconds(15)).header("Content-Type", "application/json")
            .header("Accept", "application/json").header("User-Agent", "Buggcraft-Translation-SDK/1.0 Java");
        headers.forEach(builder::header);
        if (body == null) builder.GET();
        else builder.POST(HttpRequest.BodyPublishers.ofString(JSON.writeValueAsString(body), StandardCharsets.UTF_8));
        var response = HTTP.send(builder.build(), HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
        if (response.statusCode() >= 400) throw new IllegalStateException("HTTP " + response.statusCode() + ": " + response.body());
        return JSON.readTree(response.body());
    }
    static String textHash(String text) throws Exception {
        // TreeMap orders ASCII keys; Jackson writes Unicode as UTF-8 by default.
        var envelope = new TreeMap<String,String>();
        envelope.put("field", "summary"); envelope.put("format", "plain");
        envelope.put("schema", "mc-text-v1"); envelope.put("source_language", "und");
        envelope.put("text", text.replace("\r\n", "\n").replace("\r", "\n"));
        return "sha256:" + HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(JSON.writeValueAsBytes(envelope)));
    }
    public static void main(String[] args) throws Exception {
        String id = args.length > 0 ? args[0] : "AANobbMI";
        var source = call("projects/modrinth/" + id + "/source", null, Map.of());
        String hash = textHash(source.path("fields").path("summary").path("text").asText());
        if (!hash.equals(source.path("fields").path("summary").path("source_hash").asText())) throw new IllegalStateException("Hash mismatch");
        var payload = new HashMap<String,Object>(Map.of("target_language", args.length > 1 ? args[1] : "zh-Hans", "profile", "minecraft-v1", "items", List.of(Map.of(
            "request_id", "demo", "resource", source.get("resource"), "fields", List.of(Map.of("field", "summary", "source_hash", hash))))));
        boolean ensured = false;
        for (int i=0; i<60; i++) {
            var field = call("translations:resolve", payload, Map.of()).path("results").get(0).path("fields").get(0);
            if (field.path("update").path("action").asText().equals("refresh_source")) throw new IllegalStateException("Reload upstream source and restart");
            if (field.path("state").asText().equals("ready") && field.path("matches_request").asBoolean() && field.path("is_current").asBoolean()) {
                System.out.println(field.path("translation").path("text").asText()); return;
            }
            System.err.println(field.path("update").path("notice").asText());
            if (Set.of("failed", "blocked").contains(field.path("state").asText())) throw new IllegalStateException(field.toString());
            if (!ensured) {
                String token = call("sessions/anonymous", Map.of("client_id", "java-example"), Map.of()).path("access_token").asText();
                var ensure = new HashMap<>(payload); ensure.put("purpose", "visible_list");
                call("translations:ensure", ensure, Map.of("Authorization", "Bearer " + token, "Idempotency-Key", UUID.randomUUID().toString())); ensured = true;
            }
            Thread.sleep(2000);
        }
        throw new IllegalStateException("Still translating: keep original visible and retry later");
    }
}
