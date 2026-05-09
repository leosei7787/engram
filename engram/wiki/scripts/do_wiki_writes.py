import base64,os
BASE=os.environ.get("ENGRAM_WIKI_PATH", os.path.join(os.path.dirname(__file__), "../../..")) + "/wiki"
def w(p,b64c):
  open(os.path.join(BASE,p),"w").write(base64.b64decode(b64c).decode())
  print("OK:",p[:50])
