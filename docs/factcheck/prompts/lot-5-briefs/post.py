import json,sys
p=sys.argv[1]; d=json.load(open(p))
d["posts"].append({"id":sys.argv[2],"author_id":sys.argv[3],"lang":sys.argv[4],"text":sys.argv[5],"created_at":"2026-11-11T10:00:00.000Z"})
json.dump(d,open(p,"w"))
