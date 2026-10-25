/* C11: libcurl, cJSON, OpenSSL. Build with the accompanying CMakeLists.txt. */
#include <curl/curl.h>
#include <cjson/cJSON.h>
#include <openssl/sha.h>
#include <openssl/rand.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#ifdef _WIN32
#include <windows.h>
#define pause_two_seconds() Sleep(2000)
#else
#include <unistd.h>
#define pause_two_seconds() sleep(2)
#endif

static const char *base;
typedef struct { char *data; size_t size; } Buffer;
static void fail(const char *message) { fprintf(stderr, "%s\n", message); exit(1); }
static size_t receive(char *data, size_t size, size_t count, void *context) {
    Buffer *buffer = context; size_t bytes = size * count;
    if (bytes > 1048576 || buffer->size > 1048576 - bytes) return 0;
    char *next = realloc(buffer->data, buffer->size + bytes + 1);
    if (!next) return 0;
    buffer->data = next; memcpy(next + buffer->size, data, bytes);
    buffer->size += bytes; next[buffer->size] = '\0'; return bytes;
}
static cJSON *call(const char *path, cJSON *body, const char *authorization, const char *idempotency) {
    CURL *curl = curl_easy_init(); if (!curl) fail("curl init failed");
    Buffer buffer = {NULL, 0}; char url[2048];
    if (snprintf(url, sizeof url, "%s/%s", base, path) >= (int)sizeof url) fail("URL too long");
    struct curl_slist *headers = curl_slist_append(NULL, "Content-Type: application/json");
    if (authorization) headers = curl_slist_append(headers, authorization);
    if (idempotency) headers = curl_slist_append(headers, idempotency);
    char *raw = body ? cJSON_PrintUnformatted(body) : NULL;
    curl_easy_setopt(curl, CURLOPT_URL, url); curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
    curl_easy_setopt(curl, CURLOPT_USERAGENT, "Buggcraft-Translation-SDK/1.0 C");
    curl_easy_setopt(curl, CURLOPT_TIMEOUT, 15L); curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT, 5L);
    curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 0L); curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, receive);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &buffer);
    if (raw) curl_easy_setopt(curl, CURLOPT_POSTFIELDS, raw);
    CURLcode code = curl_easy_perform(curl); long status = 0;
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &status);
    curl_slist_free_all(headers); curl_easy_cleanup(curl); cJSON_free(raw);
    if (code != CURLE_OK || status >= 400) { free(buffer.data); fail("API request failed; retain original and apply retry/backoff"); }
    cJSON *result = cJSON_Parse(buffer.data ? buffer.data : ""); free(buffer.data);
    if (!result) fail("Invalid JSON response"); return result;
}
static cJSON *get(cJSON *object, const char *key) {
    cJSON *value = cJSON_GetObjectItemCaseSensitive(object, key);
    if (!value) fail("Missing response field"); return value;
}
static const char *text(cJSON *object, const char *key) {
    const char *value = cJSON_GetStringValue(get(object, key));
    if (!value) fail("Expected JSON string"); return value;
}
static void text_hash(const char *original, char hash[72]) {
    char *normalized = malloc(strlen(original) + 1); if (!normalized) fail("Out of memory");
    size_t j = 0;
    for (size_t i = 0; original[i]; i++) {
        if (original[i] == '\r') { normalized[j++] = '\n'; if (original[i+1] == '\n') i++; }
        else normalized[j++] = original[i];
    }
    normalized[j] = 0;
    /* cJSON preserves insertion order: insert these ASCII keys in sorted order. */
    cJSON *envelope = cJSON_CreateObject();
    cJSON_AddStringToObject(envelope, "field", "summary"); cJSON_AddStringToObject(envelope, "format", "plain");
    cJSON_AddStringToObject(envelope, "schema", "mc-text-v1"); cJSON_AddStringToObject(envelope, "source_language", "und");
    cJSON_AddStringToObject(envelope, "text", normalized);
    char *raw = cJSON_PrintUnformatted(envelope); unsigned char digest[SHA256_DIGEST_LENGTH];
    SHA256((const unsigned char *)raw, strlen(raw), digest); strcpy(hash, "sha256:");
    for (size_t i=0; i<sizeof digest; i++) snprintf(hash + 7 + i*2, 3, "%02x", digest[i]);
    cJSON_free(raw); cJSON_Delete(envelope); free(normalized);
}
int main(int argc, char **argv) {
    base = getenv("TRANSLATION_API"); if (!base) base = "https://translate.bugg.icu/v1";
    curl_global_init(CURL_GLOBAL_DEFAULT); const char *id = argc > 1 ? argv[1] : "AANobbMI";
    if (strlen(id) > 128) fail("Project ID too long");
    char path[256], hash[72]; snprintf(path, sizeof path, "projects/modrinth/%s/source", id);
    cJSON *source = call(path, NULL, NULL, NULL), *summary = get(get(source, "fields"), "summary");
    text_hash(text(summary, "text"), hash); if (strcmp(hash, text(summary, "source_hash"))) fail("Hash mismatch");
    cJSON *payload = cJSON_CreateObject(), *items = cJSON_AddArrayToObject(payload, "items"), *item = cJSON_CreateObject();
    cJSON_AddStringToObject(payload, "target_language", argc > 2 ? argv[2] : "zh-Hans"); cJSON_AddStringToObject(payload, "profile", "minecraft-v1");
    cJSON_AddItemToArray(items, item); cJSON_AddStringToObject(item, "request_id", "demo");
    cJSON_AddItemToObject(item, "resource", cJSON_Duplicate(get(source, "resource"), 1));
    cJSON *fields = cJSON_AddArrayToObject(item, "fields"), *field = cJSON_CreateObject();
    cJSON_AddItemToArray(fields, field); cJSON_AddStringToObject(field, "field", "summary"); cJSON_AddStringToObject(field, "source_hash", hash);
    cJSON_Delete(source); int ensured = 0;
    for (int i=0; i<60; i++) {
        cJSON *response = call("translations:resolve", payload, NULL, NULL);
        cJSON *value = cJSON_GetArrayItem(get(cJSON_GetArrayItem(get(response, "results"), 0), "fields"), 0);
        const char *state = text(value, "state"); cJSON *update = get(value, "update");
        if (!strcmp(text(update, "action"), "refresh_source")) fail("Reload upstream source and restart");
        if (!strcmp(state, "ready") && cJSON_IsTrue(get(value, "matches_request")) && cJSON_IsTrue(get(value, "is_current"))) {
            puts(text(get(value, "translation"), "text")); cJSON_Delete(response); cJSON_Delete(payload); curl_global_cleanup(); return 0;
        }
        fprintf(stderr, "%s\n", text(update, "notice"));
        if (!strcmp(state, "failed") || !strcmp(state, "blocked")) fail("Generation failed: keep original visible");
        cJSON_Delete(response);
        if (!ensured) {
            cJSON *request = cJSON_CreateObject(); cJSON_AddStringToObject(request, "client_id", "c-example");
            cJSON *session = call("sessions/anonymous", request, NULL, NULL); cJSON_Delete(request);
            char authorization[512], key[128]; unsigned char random[16];
            if (RAND_bytes(random, sizeof random) != 1) fail("RNG unavailable");
            strcpy(key, "Idempotency-Key: ");
            for (size_t j=0; j<sizeof random; j++) snprintf(key + 17 + j*2, 3, "%02x", random[j]);
            snprintf(authorization, sizeof authorization, "Authorization: Bearer %s", text(session, "access_token"));
            cJSON *ensure = cJSON_Duplicate(payload, 1); cJSON_AddStringToObject(ensure, "purpose", "visible_list");
            cJSON *accepted = call("translations:ensure", ensure, authorization, key);
            cJSON_Delete(accepted); cJSON_Delete(ensure); cJSON_Delete(session); ensured = 1;
        }
        pause_two_seconds();
    }
    cJSON_Delete(payload); curl_global_cleanup(); fail("Still translating: retry later"); return 1;
}
