// .NET 8+, no external NuGet packages. dotnet run --project Example.csproj
using System.Security.Cryptography;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Nodes;

var api = (Environment.GetEnvironmentVariable("TRANSLATION_API") ?? "https://translate.bugg.icu/v1").TrimEnd('/');
using var http = new HttpClient(new HttpClientHandler { AllowAutoRedirect = false }) { Timeout = TimeSpan.FromSeconds(15) };
http.DefaultRequestHeaders.UserAgent.ParseAdd("Buggcraft-Translation-SDK/1.0 CSharp");
var options = new JsonSerializerOptions { Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping };
async Task<JsonNode> Call(string path, object? body = null, Dictionary<string,string>? headers = null) {
    using var request = new HttpRequestMessage(body is null ? HttpMethod.Get : HttpMethod.Post, api + "/" + path);
    if (body is not null) request.Content = new StringContent(JsonSerializer.Serialize(body, options), Encoding.UTF8, "application/json");
    if (headers is not null) foreach (var h in headers) request.Headers.Add(h.Key, h.Value);
    using var response = await http.SendAsync(request);
    response.EnsureSuccessStatusCode();
    return JsonNode.Parse(await response.Content.ReadAsStringAsync())!;
}
string Quote(string value) {
    var result = new StringBuilder("\"");
    foreach (char ch in value) {
        result.Append(ch switch {
            '\"' => "\\\"", '\\' => "\\\\", '\b' => "\\b", '\t' => "\\t", '\n' => "\\n", '\f' => "\\f", '\r' => "\\r",
            _ when ch < 32 => "\\u" + ((int)ch).ToString("x4"), _ => ch.ToString() });
    }
    return result.Append('\"').ToString();
}
string Hash(string text) {
    // Explicit canonical JSON avoids serializer escaping of supplementary Unicode.
    var normalized = text.Replace("\r\n", "\n").Replace("\r", "\n");
    var envelope = "{\"field\":\"summary\",\"format\":\"plain\",\"schema\":\"mc-text-v1\",\"source_language\":\"und\",\"text\":" + Quote(normalized) + "}";
    return "sha256:" + Convert.ToHexString(SHA256.HashData(new UTF8Encoding(false, true).GetBytes(envelope))).ToLowerInvariant();
}
var id = args.Length > 0 ? args[0] : "AANobbMI";
var source = await Call("projects/modrinth/" + id + "/source");
var hash = Hash(source["fields"]!["summary"]!["text"]!.GetValue<string>());
if (hash != source["fields"]!["summary"]!["source_hash"]!.GetValue<string>()) throw new Exception("Hash mismatch");
var payload = new JsonObject {
    ["target_language"]=args.Length > 1 ? args[1] : "zh-Hans", ["profile"]="minecraft-v1",
    ["items"]=new JsonArray(new JsonObject { ["request_id"]="demo", ["resource"]=source["resource"]!.DeepClone(),
        ["fields"]=new JsonArray(new JsonObject { ["field"]="summary", ["source_hash"]=hash }) }) };
bool ensured = false;
for (int i=0; i<60; i++) {
    var result = await Call("translations:resolve", payload);
    var field = result["results"]![0]!["fields"]![0]!;
    if (field["update"]!["action"]!.GetValue<string>() == "refresh_source") throw new Exception("Reload upstream source and restart");
    var state = field["state"]!.GetValue<string>();
    if (state == "ready" && field["matches_request"]!.GetValue<bool>() && field["is_current"]!.GetValue<bool>()) {
        Console.WriteLine(field["translation"]!["text"]!.GetValue<string>()); return;
    }
    Console.Error.WriteLine(field["update"]!["notice"]!.GetValue<string>());
    if (state is "failed" or "blocked") throw new Exception(field.ToJsonString());
    if (!ensured) {
        var session = await Call("sessions/anonymous", new { client_id="csharp-example" });
        var ensure = payload.DeepClone().AsObject(); ensure["purpose"]="visible_list";
        await Call("translations:ensure", ensure, new() { ["Authorization"]="Bearer " + session["access_token"]!.GetValue<string>(), ["Idempotency-Key"]=Guid.NewGuid().ToString() });
        ensured = true;
    }
    await Task.Delay(2000);
}
throw new TimeoutException("Still translating: keep original visible and retry later");
