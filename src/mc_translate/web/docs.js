"use strict";
const examples = {
  python:{file:"client.py",requirements:"Python 3.11+ · 仅标准库",command:"python client.py AANobbMI",support:[]},
  java:{file:"Client.java",requirements:"Java 17+ · Maven · Jackson",command:"mvn compile exec:java -Dexec.args=AANobbMI",support:["pom.xml"]},
  c:{file:"client.c",requirements:"C11 · CMake · libcurl / cJSON / OpenSSL",command:"# Ubuntu / Debian: sudo apt install build-essential cmake libcurl4-openssl-dev libcjson-dev libssl-dev nlohmann-json3-dev\ncmake -S . -B build\ncmake --build build\n./build/translation_c AANobbMI",support:["CMakeLists.txt","client.cpp"]},
  cpp:{file:"client.cpp",requirements:"C++17 · CMake · libcurl / nlohmann-json / OpenSSL",command:"# 下载 C 与 C++ 两份源文件及 CMakeLists.txt\ncmake -S . -B build\ncmake --build build\n./build/translation_cpp AANobbMI",support:["CMakeLists.txt","client.c"]},
  csharp:{file:"Program.cs",requirements:".NET 8+ · 无外部 NuGet 依赖",command:"dotnet run --project Example.csproj -- AANobbMI",support:["Example.csproj"]}
};
let generation=0;
async function showExample(language){
  const token=++generation, item=examples[language];
  for(const button of document.querySelectorAll("[data-language]")){button.classList.toggle("active",button.dataset.language===language);button.setAttribute("aria-selected",String(button.dataset.language===language));}
  document.getElementById("example-requirements").textContent=item.requirements;
  document.getElementById("example-command").querySelector("code").textContent=item.command;
  const download=document.getElementById("example-download");download.href="assets/examples/"+item.file;download.download=item.file;
  const support=document.getElementById("example-support");support.replaceChildren();
  for(const name of item.support){const a=document.createElement("a");a.href="assets/examples/"+name;a.download=name;a.textContent="下载 "+name;a.className="button secondary";support.append(a);}
  const code=document.getElementById("example-code");code.textContent="正在读取示例…";
  try{const response=await fetch("assets/examples/"+item.file);if(!response.ok)throw Error();const text=await response.text();if(token===generation)code.textContent=text;}catch(_){if(token===generation)code.textContent="读取失败，请使用下载链接。";}
}
for(const button of document.querySelectorAll("[data-language]"))button.onclick=()=>showExample(button.dataset.language);
for(const pre of document.querySelectorAll("pre")){const button=document.createElement("button");button.className="copy-code secondary";button.textContent="复制";button.type="button";button.onclick=async()=>{try{await navigator.clipboard.writeText((pre.querySelector("code")||pre).textContent.replace(/复制$/, ""));button.textContent="已复制";setTimeout(()=>button.textContent="复制",1500);}catch(_){button.textContent="请选择代码复制";}};pre.append(button);}
const observer=new IntersectionObserver(entries=>{for(const entry of entries)if(entry.isIntersecting)for(const a of document.querySelectorAll(".docs-sidebar a[href^='#']"))a.classList.toggle("active",a.hash==="#"+entry.target.id);},{rootMargin:"-15% 0px -70% 0px"});for(const section of document.querySelectorAll(".developer-docs section"))observer.observe(section);
showExample("python");
