import json
from pathlib import Path
from urllib.parse import quote

paths = [
    Path("/home/zhiyong/.job-scan/output/jobs.jsonl"),
    Path("/home/zhiyong/.job-scan/history/155a1648-28d8-4553-969e-ce3f3021c9fe/jobs.jsonl"),
    Path("/home/zhiyong/.job-scan/history/8d53ee5b-dcf7-4fcd-9161-48efe7d434fc/jobs.jsonl"),
]


def detail_url(job_id: str, company: str) -> str:
    return (
        "https://simplify.jobs/jobs?query="
        + quote(company, safe="")
        + "&state=Germany&country=Germany&jobId="
        + job_id
    )


for path in paths:
    changed_top = 0
    changed_occ = 0
    out = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line:
                continue
            d = json.loads(line)
            for o in d.get("source_occurrences", []):
                if o.get("source") == "simplify":
                    oid = o.get("external_id")
                    if o.get("url", "").startswith("https://simplify.jobs/jobs?jobId="):
                        o["url"] = detail_url(oid, o.get("company") or "")
                        changed_occ += 1
            if d.get("url", "").startswith("https://simplify.jobs/jobs?jobId="):
                d["url"] = detail_url(
                    d["url"].split("jobId=")[-1],
                    d.get("company") or "",
                )
                changed_top += 1
            out.append(json.dumps(d, ensure_ascii=False))
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(path, "top:", changed_top, "occ:", changed_occ)
