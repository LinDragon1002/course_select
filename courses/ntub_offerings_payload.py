"""NTUB 真實專業選修資料的標準化。

本模組只做「讀取既有爬蟲輸出 → 整理成統一 JSON」，不重新爬蟲。

來源
----
* ``data/ntub_offerings/*.json``：官方課表查詢系統（TchSchedule_Search.aspx）逐關鍵字匯出的開課。
* ``data/ntub_curriculum_electives.json``：各入學年／專班／班級的專業選修推薦課綱。

輸出
----
``data/ntub_import_ready/offerings.json``：統一格式，可直接供後續 Django 匯入（Term / Course / Teacher / Section）。

設計原則
--------
1. 同一筆官方開課（學年＋學期＋班級代碼＋序號＋課號）只會出現一次；重複來源會被合併並記錄。
2. 課綱推薦以「課名完全相同」且「班級代碼相同」才視為對應；只差課名的近似課一律視為不同課。
   例：「財務管理(二)(技優領航專班)」與「財務管理」是兩門課，不可互相對應。
3. 校方人數（名額／已選人數）只是歷史來源資料。本系統的名額與選課人數由本地資料獨立計算，
   因此輸出中一律使用 ``official_*`` 前綴，並在 ``policy`` 說明。
"""

from __future__ import annotations

import json
import os
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: 爬蟲輸出的來源目錄（可用環境變數覆寫）。
DEFAULT_OFFERINGS_DIR = Path(
    os.environ.get(
        "NTUB_OFFERINGS_DIR",
        r"C:\Users\user\OneDrive\Desktop\course_select\coursepuzzle-main\data\ntub_offerings",
    )
)

#: 課綱推薦來源檔（可用環境變數覆寫）。
DEFAULT_CURRICULUM_FILE = Path(
    os.environ.get(
        "NTUB_CURRICULUM_FILE",
        r"C:\Users\user\OneDrive\Desktop\course_select\coursepuzzle-main\data\ntub_curriculum_electives.json",
    )
)

#: 標準化結果的輸出位置。
DEFAULT_OUTPUT_PATH = REPO_ROOT / "data" / "ntub_import_ready" / "offerings.json"

SCHEMA_VERSION = "1.0"

WEEKDAY_LABELS = {1: "週一", 2: "週二", 3: "週三", 4: "週四", 5: "週五", 6: "週六", 7: "週日"}

DEDUP_KEY_FIELDS = ("academic_year", "semester", "class_code", "serial", "course_code")

POLICY = OrderedDict(
    [
        (
            "official_capacity",
            "校方公告名額（cap），可作為匯入時 Section.capacity 的初始值；"
            "本地額滿與選課人數仍以本系統的加選紀錄為準。",
        ),
        (
            "official_enrolled",
            "校方公告的已選人數，僅作歷史來源資料；本地選課人數由 Enrollment 統計獨立計算。",
        ),
        ("local_enrollment", "本地選課人數一律以本系統的加選紀錄統計，與 official_enrolled 無關。"),
        ("dedup_key", list(DEDUP_KEY_FIELDS)),
        (
            "recommendation_match",
            "課綱推薦以「課名完全相同」對應到官方開課；班級代碼相同為完全對應（matched），"
            "班級代碼不同仍連結同一門課但標記為 course_name_only 並列為疑義。"
            "近似課名（例如「財務管理(二)(技優領航專班)」與「財務管理」）視為不同課程，互不取代。",
        ),
    ]
)


class PayloadError(RuntimeError):
    """來源資料不符合預期時拋出。"""


# --------------------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------------------
def _as_text(value):
    if value is None:
        return ""
    return str(value).strip()


def _as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value, default=0):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _term_code(academic_year, semester):
    return f"{_as_text(academic_year)}-{_as_text(semester)}"


def offering_key(academic_year, semester, class_code, serial, course_code):
    """官方開課的唯一鍵：學年＋學期＋班級代碼＋序號＋課號。"""
    return "|".join(
        [_as_text(academic_year), _as_text(semester), _as_text(class_code), _as_text(serial), _as_text(course_code)]
    )


def section_label(class_code, serial):
    """從班級代碼推導班別標籤（40620A → A），推導不出來時退回序號。"""
    code = _as_text(class_code)
    if code and code[-1].isalpha():
        return code[-1].upper()
    return _as_text(serial)


def _utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------------------
# 讀取來源
# --------------------------------------------------------------------------------------
def load_offering_documents(source_dir=None, with_skipped=False):
    """讀取來源目錄下所有爬蟲輸出的 JSON 文件。

    只接受看起來像「開課匯出」的文件（含 ``offerings`` 鍵的物件），
    來源目錄裡的其他 JSON（例如課綱推薦檔）會被略過。
    """
    directory = Path(source_dir or DEFAULT_OFFERINGS_DIR)
    if not directory.is_dir():
        raise PayloadError(f"來源目錄不存在：{directory}")
    documents = []
    skipped = []
    for path in sorted(directory.glob("*.json")):
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict) or "offerings" not in data:
            skipped.append(path.name)
            continue
        documents.append({"path": path, "data": data})
    if not documents:
        raise PayloadError(f"來源目錄內沒有開課匯出 JSON：{directory}")
    if with_skipped:
        return documents, skipped
    return documents


def load_curriculum(curriculum_file=None):
    """讀取課綱推薦檔（清單格式）。"""
    path = Path(curriculum_file or DEFAULT_CURRICULUM_FILE)
    if not path.is_file():
        raise PayloadError(f"課綱推薦檔不存在：{path}")
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, dict):
        data = data.get("recommendations") or data.get("items") or []
    if not isinstance(data, list):
        raise PayloadError(f"課綱推薦檔格式非預期：{path}")
    return data


# --------------------------------------------------------------------------------------
# 官方開課
# --------------------------------------------------------------------------------------
def _build_offering(document, raw):
    """把單筆爬蟲輸出轉成統一格式的開課紀錄。"""
    data = document["data"]
    request = data.get("request") or {}
    echo = data.get("echo") or {}
    academic_year = _as_text(raw.get("academic_year") or echo.get("academic_year") or request.get("academic_year"))
    semester = _as_text(raw.get("semester") or echo.get("semester") or request.get("semester"))
    class_code = _as_text(raw.get("class_code"))
    serial = _as_text(raw.get("serial"))
    course_code = _as_text(raw.get("course_code"))

    periods = [int(p) for p in (raw.get("periods") or []) if str(p).strip().isdigit()]
    periods = sorted(periods)
    weekday = _as_int(raw.get("weekday"), 0)

    teacher = {
        "id": _as_text(raw.get("teacher_id")),
        "name": _as_text(raw.get("teacher_name")),
        "raw": _as_text(raw.get("teacher_raw")),
    }

    classroom = {
        "code": _as_text(raw.get("classroom_code")),
        "name": _as_text(raw.get("classroom")),
        "raw": _as_text(raw.get("schedule_raw")),
    }

    credits = _as_float(raw.get("credits"), 0.0)
    hours = _as_float(raw.get("hours"), 0.0)
    official_capacity = _as_int(raw.get("capacity"), 0)
    official_enrolled = _as_int(raw.get("enrolled_official"), 0)

    offering = OrderedDict(
        [
            ("key", offering_key(academic_year, semester, class_code, serial, course_code)),
            ("academic_year", academic_year),
            ("semester", semester),
            ("term_code", _term_code(academic_year, semester)),
            ("class_code", class_code),
            ("class_name", _as_text(raw.get("class_name"))),
            ("serial", serial),
            ("course_code", course_code),
            ("course_name", _as_text(raw.get("course_name"))),
            ("teacher", teacher),
            (
                "schedule",
                OrderedDict(
                    [
                        ("weekday", weekday),
                        ("weekday_label", WEEKDAY_LABELS.get(weekday, "")),
                        ("periods", periods),
                        ("start_period", periods[0] if periods else 0),
                        ("end_period", periods[-1] if periods else 0),
                        ("raw", _as_text(raw.get("schedule_raw"))),
                    ]
                ),
            ),
            ("classroom", classroom),
            ("credits", credits),
            ("credits_raw", _as_text(raw.get("credits_raw"))),
            ("hours", hours),
            ("hours_raw", _as_text(raw.get("hours_raw"))),
            ("category", _as_text(raw.get("category"))),
            ("elective_category", _as_text(raw.get("category"))),
            ("official_capacity", official_capacity),
            ("official_enrolled", official_enrolled),
            ("official_capacity_raw", _as_text(raw.get("capacity_raw"))),
            ("capacity_is_historical_only", True),
            ("notes", list(raw.get("notes") or [])),
            ("matched_keywords", []),
            ("source", OrderedDict()),
            ("django", OrderedDict()),
        ]
    )
    offering["django"] = OrderedDict(
        [
            ("term_code", offering["term_code"]),
            ("course_code", course_code),
            ("course_name", offering["course_name"]),
            ("teacher_name", teacher["name"]),
            ("credits", int(credits) if credits.is_integer() else credits),
            ("section_label", section_label(class_code, serial)),
            ("day", weekday),
            ("start_period", offering["schedule"]["start_period"]),
            ("end_period", offering["schedule"]["end_period"]),
            ("room", classroom["code"]),
            ("classroom_name", classroom["name"]),
            # 名額（cap）是班級額滿依據，匯入時作為 Section.capacity；已選人數只作歷史參考。
            ("capacity", official_capacity),
            ("official_capacity", official_capacity),
            ("official_enrolled", official_enrolled),
            ("capacity_source", "official_quota"),
        ]
    )
    offering["source"] = OrderedDict(
        [
            ("file", document["path"].name),
            ("keyword", _as_text(request.get("keyword"))),
            ("source_url", _as_text(data.get("source_url"))),
            ("retrieved_at", _as_text(data.get("retrieved_at"))),
            ("exported_at", _as_text(data.get("exported_at"))),
        ]
    )
    return offering


def collect_offerings(documents):
    """彙整所有官方開課並依唯一鍵去重。

    回傳 ``(offerings, duplicates, ambiguities)``：
    * ``offerings``：去重後的開課（依鍵排序）。
    * ``duplicates``：被合併的重複來源紀錄。
    * ``ambiguities``：合併時產生的說明。
    """
    merged = OrderedDict()
    duplicates = []
    ambiguities = []

    for document in documents:
        keyword = _as_text((document["data"].get("request") or {}).get("keyword"))
        for raw in document["data"].get("offerings") or []:
            offering = _build_offering(document, raw)
            key = offering["key"]
            if keyword and keyword not in offering["matched_keywords"]:
                offering["matched_keywords"].append(keyword)
            if key in merged:
                existing = merged[key]
                for kw in offering["matched_keywords"]:
                    if kw not in existing["matched_keywords"]:
                        existing["matched_keywords"].append(kw)
                existing_files = existing["source"].setdefault("files", [])
                if offering["source"]["file"] not in existing_files:
                    existing_files.append(offering["source"]["file"])
                duplicates.append(
                    OrderedDict(
                        [
                            ("key", key),
                            ("course_name", offering["course_name"]),
                            ("class_code", offering["class_code"]),
                            ("serial", offering["serial"]),
                            ("kept_from", existing["source"]["file"]),
                            ("merged_from", offering["source"]["file"]),
                            ("keyword", offering["source"]["keyword"]),
                        ]
                    )
                )
                ambiguities.append(
                    {
                        "kind": "duplicate_offering_merged",
                        "message": f"官方開課 {key} 在多個來源出現，已合併為一筆。",
                        "key": key,
                        "files": list(existing_files),
                    }
                )
                continue
            merged[key] = offering

    offerings = [merged[key] for key in sorted(merged)]
    for offering in offerings:
        offering["source"].setdefault("files", [offering["source"]["file"]])
    return offerings, duplicates, ambiguities


# --------------------------------------------------------------------------------------
# 課綱推薦
# --------------------------------------------------------------------------------------
def _candidate_course_code(candidates, matched):
    """對應到的課號：完全對應時取該筆；只有同名候選時取唯一候選的課號。"""
    if matched:
        return matched["course_code"]
    if len(candidates) == 1:
        return candidates[0]["course_code"]
    return ""


def _build_recommendation(raw, index):
    """把一筆課綱推薦轉成統一格式。

    對應規則：課名 **完全相同** 才視為同一門課；班級代碼相同時為完全對應，
    班級代碼不同則仍連到該門課，但標記為班別不一致並記錄為疑義。
    任何近似課名（例如「財務管理(二)(技優領航專班)」對「財務管理」）都不視為對應。
    """
    admission_year = _as_text(raw.get("admission_year"))
    class_code = _as_text(raw.get("class_code"))
    course_name = _as_text(raw.get("course_name"))

    candidates = index.get(course_name, [])
    exact = [offering for offering in candidates if offering["class_code"] == class_code]

    if exact:
        status = "matched"
        matched = sorted(exact, key=lambda o: o["key"])[0]
        offering_key_value = matched["key"]
    elif len(candidates) == 1:
        # 同名但班別不同：課程是同一門，班別不同（例如技優班在一般班的開課）。保留連結並標記疑義。
        status = "course_name_only"
        matched = candidates[0]
        offering_key_value = matched["key"]
    elif candidates:
        status = "ambiguous_candidates"
        matched = None
        offering_key_value = None
    else:
        status = "no_offering"
        matched = None
        offering_key_value = None

    recommendation = OrderedDict(
        [
            ("admission_year", admission_year),
            ("track", _as_text(raw.get("track"))),
            ("program_code", _as_text(raw.get("program_code"))),
            ("program_name", _as_text(raw.get("program_name"))),
            ("department_code", _as_text(raw.get("department_code"))),
            ("department_name", _as_text(raw.get("department_name"))),
            ("grade", _as_int(raw.get("grade"), 0)),
            ("semester", _as_int(raw.get("semester"), 0)),
            ("class_code", class_code),
            ("course_name", course_name),
            ("match_status", status),
            ("offering_key", offering_key_value),
            ("course_code", _candidate_course_code(candidates, matched)),
            ("matched_course_code", matched["course_code"] if matched else ""),
            ("matched_offering_class_code", matched["class_code"] if matched else ""),
            (
                "candidates",
                [
                    OrderedDict(
                        [
                            ("offering_key", offering["key"]),
                            ("course_code", offering["course_code"]),
                            ("class_code", offering["class_code"]),
                            ("class_name", offering["class_name"]),
                            ("course_name", offering["course_name"]),
                        ]
                    )
                    for offering in sorted(candidates, key=lambda o: o["key"])
                ],
            ),
        ]
    )
    return recommendation


def match_recommendations(curriculum, offerings):
    """把課綱推薦與官方開課對應，並記錄無法對應的情況。"""
    index = {}
    for offering in offerings:
        index.setdefault(offering["course_name"], []).append(offering)

    recommendations = []
    ambiguities = []
    for raw in curriculum:
        recommendation = _build_recommendation(raw, index)
        recommendations.append(recommendation)

        if recommendation["match_status"] == "matched":
            continue

        if recommendation["match_status"] == "course_name_only":
            candidate = recommendation["candidates"][0]
            ambiguities.append(
                {
                    "kind": "recommendation_class_mismatch",
                    "message": (
                        f"{recommendation['admission_year']} {recommendation['track']} "
                        f"班級 {recommendation['class_code']} 推薦「{recommendation['course_name']}」，"
                        f"官方開課屬班級 {candidate['class_code']}（課號 {candidate['course_code']}）；"
                        f"視為同一門課但班別不同，需人工確認。"
                    ),
                    "admission_year": recommendation["admission_year"],
                    "track": recommendation["track"],
                    "class_code": recommendation["class_code"],
                    "course_name": recommendation["course_name"],
                    "candidates": recommendation["candidates"],
                }
            )
        elif recommendation["match_status"] == "ambiguous_candidates":
            ambiguities.append(
                {
                    "kind": "recommendation_ambiguous_candidates",
                    "message": (
                        f"{recommendation['admission_year']} {recommendation['track']} "
                        f"班級 {recommendation['class_code']} 推薦「{recommendation['course_name']}」，"
                        f"同名官方開課不唯一，未自動對應。"
                    ),
                    "admission_year": recommendation["admission_year"],
                    "track": recommendation["track"],
                    "class_code": recommendation["class_code"],
                    "course_name": recommendation["course_name"],
                    "candidates": recommendation["candidates"],
                }
            )
        else:
            ambiguities.append(
                {
                    "kind": "recommendation_without_offering",
                    "message": (
                        f"{recommendation['admission_year']} {recommendation['track']} "
                        f"班級 {recommendation['class_code']} 推薦「{recommendation['course_name']}」，"
                        f"來源內查無同名官方開課。"
                    ),
                    "admission_year": recommendation["admission_year"],
                    "track": recommendation["track"],
                    "class_code": recommendation["class_code"],
                    "course_name": recommendation["course_name"],
                    "candidates": [],
                }
            )

    # 有官方開課但沒有任何課綱推薦到的課（例如近似課名），明確標記避免被誤認。
    referenced = {rec["course_name"] for rec in recommendations}
    for offering in offerings:
        if offering["course_name"] in referenced:
            continue
        ambiguities.append(
            {
                "kind": "offering_without_recommendation",
                "message": (
                    f"「{offering['course_name']}」（班級 {offering['class_code']}，課號 {offering['course_code']}）"
                    f"沒有任何課綱推薦指向它；此課與同名近似課為不同課程，匯入時不可互相取代。"
                ),
                "course_name": offering["course_name"],
                "class_code": offering["class_code"],
                "course_code": offering["course_code"],
                "offering_key": offering["key"],
            }
        )
    return recommendations, ambiguities


# --------------------------------------------------------------------------------------
# 驗證
# --------------------------------------------------------------------------------------
def validate_payload(payload):
    """檢查輸出一致性；違反規則時拋出 PayloadError。"""
    offerings = payload["offerings"]
    keys = [offering["key"] for offering in offerings]
    if len(keys) != len(set(keys)):
        raise PayloadError("官方開課有重複的 key。")

    by_key = {offering["key"]: offering for offering in offerings}
    by_name = {}
    for offering in offerings:
        by_name.setdefault(offering["course_name"], []).append(offering)

    for recommendation in payload["curriculum_recommendations"]:
        key = recommendation["offering_key"]
        if not key:
            continue
        offering = by_key.get(key)
        if offering is None:
            raise PayloadError(f"課綱推薦指向不存在的開課：{key}")
        if offering["course_name"] != recommendation["course_name"]:
            raise PayloadError(
                f"課綱推薦「{recommendation['course_name']}」錯配到「{offering['course_name']}」。"
            )
        if recommendation["match_status"] == "matched" and offering["class_code"] != recommendation["class_code"]:
            raise PayloadError(
                f"課綱推薦「{recommendation['course_name']}」的班級 {recommendation['class_code']} "
                f"與開課班級 {offering['class_code']} 不符。"
            )

    # 近似課名不得共用同一筆開課（財務管理 vs 財務管理(二)(技優領航專班)）。
    for name, group in by_name.items():
        codes = {offering["course_code"] for offering in group}
        if len(codes) > 1:
            raise PayloadError(f"課名「{name}」對應到多個課號：{sorted(codes)}")

    for offering in offerings:
        if "enrolled" in offering:
            raise PayloadError("本地選課人數不可寫入校方資料欄位。")
        if not offering.get("capacity_is_historical_only", False):
            raise PayloadError("校方人數必須標記為僅供歷史參考。")
    return True


# --------------------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------------------
def build_payload(source_dir=None, curriculum_file=None, generated_at=None):
    """讀取來源並組出標準化 payload。"""
    documents, skipped = load_offering_documents(source_dir, with_skipped=True)
    curriculum = load_curriculum(curriculum_file)

    offerings, duplicates, offer_ambiguities = collect_offerings(documents)
    recommendations, match_ambiguities = match_recommendations(curriculum, offerings)

    ambiguities = list(offer_ambiguities) + list(match_ambiguities)
    # 保留來源檔本身標記的部分比對說明（例如關鍵字命中近似課名）。
    for document in documents:
        for note in document["data"].get("ambiguities") or []:
            ambiguities.append(
                {
                    "kind": "source_partial_keyword_match",
                    "message": _as_text(note),
                    "file": document["path"].name,
                }
            )

    keywords = []
    for document in documents:
        keyword = _as_text((document["data"].get("request") or {}).get("keyword"))
        if keyword and keyword not in keywords:
            keywords.append(keyword)

    retrieved_times = [
        _as_text(document["data"].get("retrieved_at"))
        for document in documents
        if _as_text(document["data"].get("retrieved_at"))
    ]

    payload = OrderedDict(
        [
            ("schema_version", SCHEMA_VERSION),
            ("generated_by", "manage.py prepare_ntub_offerings"),
            ("generated_at", generated_at or _utc_now_iso()),
            (
                "source",
                OrderedDict(
                    [
                        ("offerings_dir", str(Path(source_dir or DEFAULT_OFFERINGS_DIR))),
                        ("curriculum_file", str(Path(curriculum_file or DEFAULT_CURRICULUM_FILE))),
                        (
                            "schedule_source_url",
                            _as_text(documents[0]["data"].get("source_url")),
                        ),
                        ("source_files", [document["path"].name for document in documents]),
                        ("skipped_files", skipped),
                        ("keywords", keywords),
                        ("retrieved_at_min", min(retrieved_times) if retrieved_times else ""),
                        ("retrieved_at_max", max(retrieved_times) if retrieved_times else ""),
                        ("rescraped", False),
                    ]
                ),
            ),
            ("policy", POLICY),
            (
                "stats",
                OrderedDict(
                    [
                        ("source_files", len(documents)),
                        ("keywords", len(keywords)),
                        ("raw_offerings", sum(len(d["data"].get("offerings") or []) for d in documents)),
                        ("unique_offerings", len(offerings)),
                        ("duplicates_merged", len(duplicates)),
                        ("curriculum_recommendations", len(recommendations)),
                        (
                            "recommendations_matched",
                            sum(1 for r in recommendations if r["match_status"] == "matched"),
                        ),
                        (
                            "recommendations_linked_other_class",
                            sum(1 for r in recommendations if r["match_status"] == "course_name_only"),
                        ),
                        (
                            "recommendations_unlinked",
                            sum(1 for r in recommendations if r["match_status"] in
                                ("no_offering", "ambiguous_candidates")),
                        ),
                        ("ambiguities", len(ambiguities)),
                    ]
                ),
            ),
            ("offerings", offerings),
            ("curriculum_recommendations", recommendations),
            ("ambiguities", ambiguities),
        ]
    )
    validate_payload(payload)
    return payload


def write_payload(payload, output_path=None):
    """把 payload 寫成 JSON 檔（UTF-8、保留中文、結尾換行）。"""
    path = Path(output_path or DEFAULT_OUTPUT_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return path


def payload_without_timestamp(payload):
    """去掉會變動的時間欄位，方便比對輸出是否一致。"""
    trimmed = OrderedDict(payload)
    trimmed.pop("generated_at", None)
    return trimmed
