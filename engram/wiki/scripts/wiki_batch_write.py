import json,os,sys
manifest_path=sys.argv[1]
BASE=os.environ.get("ENGRAM_WIKI_PATH", os.path.join(os.path.dirname(__file__), "../../..")) + "/wiki"
with open(manifest_path) as f:pages=json.load(f)
for rel,content in pages.items():
 full=os.path.join(BASE,rel)
 os.makedirs(os.path.dirname(full),exist_ok=True)
 open(full,"w").write(content)
 print("Written:",rel)
print("Done:",len(pages),"pages")