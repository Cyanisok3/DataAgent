"""固定官方输入版本，拆分模型可见题目与评分 oracle；不推导指标口径。"""
import argparse
import csv
import hashlib
import io
import json
import re
import sqlite3
from itertools import zip_longest
from pathlib import Path
from urllib.request import urlopen

from datasource import DataSource
from semantic_layer import Domain, Table

DATA_REVISION = "f65faf4ae3b638c1fa6df1d3370c8d92c8366301"
SCORER_REVISION = "abd11b6db92a1c9f809b32f7564c7c71b34d67f0"
ASSETS = {
    "questions.jsonl": (
        f"https://huggingface.co/datasets/birdsql/bird_mini_dev/resolve/{DATA_REVISION}/data/mini_dev_sqlite-00000-of-00001.json",
        "88ceb0710163cae46a256ecea8f0a8c98286599530b60587fda5c3cfe57d45d2"),
    "evaluation_ex.py": (
        f"https://raw.githubusercontent.com/bird-bench/mini_dev/{SCORER_REVISION}/evaluation/evaluation_ex.py",
        "da1bbcd4530be83692d7c650c814ea9704bb710d0c953eb75d02ccb38233cf89"),
    "evaluation_utils.py": (
        f"https://raw.githubusercontent.com/bird-bench/mini_dev/{SCORER_REVISION}/evaluation/evaluation_utils.py",
        "f6943d249caac5aeaef9bce21d43dbf29dcef85a0c965a76df032a9542f308bf"),
}


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value):
    # 不覆盖既有运行或版本文件，避免不知不觉改变评测输入。
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)


def prepare(destination: Path):
    destination.mkdir(parents=True, exist_ok=True)
    for name, (url, expected) in ASSETS.items():
        path = destination / name
        if not path.exists():
            with urlopen(url, timeout=30) as response:
                payload = response.read(2_000_001)
            if hashlib.sha256(payload).hexdigest() != expected:
                raise ValueError(f"asset_hash_mismatch: {name}")
            with path.open("xb") as stream:
                stream.write(payload)
        if file_hash(path) != expected:
            raise ValueError(f"asset_hash_mismatch: {name}")
    raw = (destination / "questions.jsonl").read_text()
    try:
        questions = json.loads(raw)
    except json.JSONDecodeError:
        questions = [json.loads(line) for line in raw.splitlines() if line.strip()]
    if len(questions) != 500 or len({q["db_id"] for q in questions}) != 11:
        raise ValueError("expected_classic_500_questions_11_databases")
    public = [{k: q[k] for k in ("question_id", "db_id", "question", "evidence")} for q in questions]
    # 轮转取各库前两题，再恢复官方顺序；不用答案或难度挑题。
    by_database: dict[str, list] = {}
    for q in public:
        by_database.setdefault(q["db_id"], []).append(q)
    ordered = [q for row in zip_longest(*(by_database[k] for k in sorted(by_database)))
               for q in row if q is not None]
    selected = {q["question_id"] for q in ordered[:20]}
    outputs = {"prompts.json": public, "oracle.json": questions,
               "debug_ids.json": [q["question_id"] for q in public if q["question_id"] in selected]}
    for name, value in outputs.items():
        path = destination / name
        if path.exists():
            if json.loads(path.read_text()) != value:
                raise ValueError(f"frozen_input_changed: {name}")
        else:
            write_json(path, value)
    return {"data_revision": DATA_REVISION, "scorer_revision": SCORER_REVISION,
            "questions": 500, "databases": 11, "debug_ids": outputs["debug_ids.json"]}


def database_path(root: Path, db_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_]+", db_id):
        raise ValueError("invalid_db_id")
    path = (root / db_id / f"{db_id}.sqlite").resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError(f"missing_or_external_database: {db_id}")
    return path


def _descriptions(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    payload = path.read_bytes()
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = payload.decode("cp1252")
    return {row["original_column_name"].strip(): "; ".join(
        f"{k}: {v}" for k, v in row.items() if k != "original_column_name" and v and v.strip())
        for row in csv.DictReader(io.StringIO(text)) if row.get("original_column_name")}


def load_source(root: Path, db_id: str) -> DataSource:
    path = database_path(root, db_id)
    tables = []
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for name in names:
            quoted = '"' + name.replace('"', '""') + '"'
            columns = conn.execute(f"PRAGMA table_info({quoted})").fetchall()
            foreign = conn.execute(f"PRAGMA foreign_key_list({quoted})").fetchall()
            metadata = path.parent / "database_description" / f"{name}.csv"
            if not metadata.resolve().is_relative_to(path.parent.resolve()):
                raise ValueError("external_schema_description")
            descriptions = _descriptions(metadata)
            for _, column, type_, _, _, pk in columns:
                descriptions[column] = f"type={type_}; primary_key={bool(pk)}; " + descriptions.get(column, "")
            for fk in foreign:
                descriptions[fk[3]] += f"; references {fk[2]}.{fk[4]}"
            tables.append(Table(name=name, description=name, domain_key=db_id,
                                columns=[c[1] for c in columns], is_visible=True,
                                keywords=[], column_descriptions=descriptions))
    return DataSource(path=path, tables=tables,
                      domains=[Domain(db_id, db_id, "当前数据库目录，无预设业务指标", [])])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "data" / "bird-mini-dev")
    args = parser.parse_args()
    print(json.dumps(prepare(args.output), ensure_ascii=False, indent=2))
