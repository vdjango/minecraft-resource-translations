// C++17: libcurl, OpenSSL, nlohmann/json. Build with CMakeLists.txt.
#include <curl/curl.h>
#include <openssl/rand.h>
#include <openssl/sha.h>
#include <nlohmann/json.hpp>
#include <chrono>
#include <cstdlib>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
using Json = nlohmann::json;
std::string base;
size_t receive(char *data, size_t size, size_t count, void *ctx) {
    auto &buffer = *static_cast<std::string *>(ctx); auto bytes = size*count;
    if (bytes > 1048576 || buffer.size() > 1048576-bytes) return 0;
    buffer.append(data, bytes); return bytes;
}
Json call(const std::string &path, const Json *body=nullptr, const std::string &token="", const std::string &key="") {
    CURL *curl = curl_easy_init(); if (!curl) throw std::runtime_error("curl init failed");
    std::string response, url=base+"/"+path, raw=body?body->dump():"";
    auto headers = curl_slist_append(nullptr, "Content-Type: application/json");
    if (!token.empty()) headers=curl_slist_append(headers,("Authorization: Bearer "+token).c_str());
    if (!key.empty()) headers=curl_slist_append(headers,("Idempotency-Key: "+key).c_str());
    curl_easy_setopt(curl,CURLOPT_URL,url.c_str()); curl_easy_setopt(curl,CURLOPT_HTTPHEADER,headers);
    curl_easy_setopt(curl,CURLOPT_USERAGENT,"Buggcraft-Translation-SDK/1.0 C++");
    curl_easy_setopt(curl,CURLOPT_TIMEOUT,15L); curl_easy_setopt(curl,CURLOPT_CONNECTTIMEOUT,5L);
    curl_easy_setopt(curl,CURLOPT_FOLLOWLOCATION,0L); curl_easy_setopt(curl,CURLOPT_WRITEFUNCTION,receive); curl_easy_setopt(curl,CURLOPT_WRITEDATA,&response);
    if(body)curl_easy_setopt(curl,CURLOPT_POSTFIELDS,raw.c_str());
    auto code=curl_easy_perform(curl); long status=0;curl_easy_getinfo(curl,CURLINFO_RESPONSE_CODE,&status);
    curl_slist_free_all(headers);curl_easy_cleanup(curl);
    if(code!=CURLE_OK||status>=400)throw std::runtime_error("API error: keep original visible and retry with backoff");
    return Json::parse(response);
}
std::string hash_text(std::string text) {
    std::string normalized;for(size_t i=0;i<text.size();i++){if(text[i]=='\r'){normalized+='\n';if(i+1<text.size()&&text[i+1]=='\n')i++;}else normalized+=text[i];}
    // Default json uses sorted std::map keys; dump() preserves UTF-8 Unicode.
    auto raw=Json{{"field","summary"},{"format","plain"},{"schema","mc-text-v1"},{"source_language","und"},{"text",normalized}}.dump();
    unsigned char digest[SHA256_DIGEST_LENGTH];SHA256(reinterpret_cast<const unsigned char*>(raw.data()),raw.size(),digest);
    std::ostringstream out;out<<"sha256:"<<std::hex<<std::setfill('0');for(auto b:digest)out<<std::setw(2)<<static_cast<unsigned>(b);return out.str();
}
int main(int argc,char **argv) {
    curl_global_init(CURL_GLOBAL_DEFAULT);
    try {
        base=std::getenv("TRANSLATION_API")?std::getenv("TRANSLATION_API"):"https://translate.bugg.icu/v1";
        auto id=argc>1?argv[1]:"AANobbMI";auto source=call("projects/modrinth/"+std::string(id)+"/source");
        auto hash=hash_text(source.at("fields").at("summary").at("text").get<std::string>());
        if(hash!=source["fields"]["summary"]["source_hash"])throw std::runtime_error("Hash mismatch");
        Json payload={{"target_language",argc>2?argv[2]:"zh-Hans"},{"profile","minecraft-v1"},{"items",Json::array({{{"request_id","demo"},{"resource",source["resource"]},{"fields",Json::array({{{"field","summary"},{"source_hash",hash}}})}}})}};
        bool ensured=false;
        for(int i=0;i<60;i++) {
            auto field=call("translations:resolve",&payload).at("results").at(0).at("fields").at(0);
            if(field["update"]["action"]=="refresh_source")throw std::runtime_error("Reload upstream source and restart");
            if(field["state"]=="ready"&&field["matches_request"]==true&&field["is_current"]==true){std::cout<<field["translation"]["text"].get<std::string>()<<'\n';curl_global_cleanup();return 0;}
            std::cerr<<field["update"]["notice"].get<std::string>()<<'\n';
            if(field["state"]=="failed"||field["state"]=="blocked")throw std::runtime_error("Translation unavailable");
            if(!ensured){Json login={{"client_id","cpp-example"}};auto token=call("sessions/anonymous",&login)["access_token"].get<std::string>();
                unsigned char random[16];if(RAND_bytes(random,sizeof random)!=1)throw std::runtime_error("RNG unavailable");
                std::ostringstream key;key<<std::hex<<std::setfill('0');for(auto b:random)key<<std::setw(2)<<static_cast<unsigned>(b);
                auto ensure=payload;ensure["purpose"]="visible_list";call("translations:ensure",&ensure,token,key.str());ensured=true;}
            std::this_thread::sleep_for(std::chrono::seconds(2));
        }
        throw std::runtime_error("Still translating: retry later");
    }catch(const std::exception &e){std::cerr<<e.what()<<'\n';curl_global_cleanup();return 1;}
}
