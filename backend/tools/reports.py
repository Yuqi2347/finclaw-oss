from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass, field
from datetime import date as date_cls, datetime
from pathlib import Path
import re
from urllib.parse import quote

from backend.core.llm_client import llm_client
from backend.core.config import BETTAFISH_MARKET_REPORT_ROOT, DATA_DIR, FINCLAW_API_BASE_URL, TRADING_REPORT_ROOT


_A_SHARE_CODE_RE = re.compile(r"^\d{6}(?:\.(?:SH|SZ|BJ))?$", re.IGNORECASE)


@dataclass(frozen=True)
class ReportArtifact:
    format: str
    internal_path: str


@dataclass(frozen=True)
class ReportRecord:
    report_id: str
    source: str
    report_type: str
    category: str
    subject: str
    date: str
    title: str
    tags: list[str] = field(default_factory=list)
    preferred_view: str = "html"
    preferred_read: str = "md"
    status: str = "complete"
    artifacts: dict[str, ReportArtifact] = field(default_factory=dict)
    freshness: str | None = None
    days_since_generated: int | None = None
    recommended_action: str | None = None


@dataclass(frozen=True)
class ReportMeta:
    report_id: str
    report_type: str
    title: str
    date: str
    format: str
    internal_path: str


class ReportLibrary:
    def list_report_catalog(
        self,
        category: str | None = None,
        report_type: str | None = None,
        source: str | None = None,
        subject: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        records = self._catalog()
        if category:
            records = [item for item in records if item.category == category]
        if report_type:
            records = [item for item in records if item.report_type == report_type]
        if source:
            normalized_source = self._normalize_source_filter(source)
            records = [item for item in records if item.source.lower() == normalized_source.lower()]
        if subject:
            selected = self._select_report_records_with_llm(records, subject, limit)
            if selected:
                records = selected
        records.sort(key=lambda item: (item.date, item.title), reverse=True)
        return [self._safe_record(item, include_artifacts=False) for item in records[:limit]]

    def get_report_detail(self, report_id: str, max_chars: int = 12000, offset: int = 0) -> dict:
        record = self.resolve_record(report_id)
        read_artifact = self._select_artifact(record, record.preferred_read)
        view_link = self._link_for_record(record)
        return {
            "report": self._safe_record(record, include_artifacts=False),
            "preferred_view_link": {
                "format": view_link["format"],
                "view_url": view_link["view_url"],
                "download_url": view_link["download_url"],
            },
            "manifest": self._report_manifest(Path(read_artifact.internal_path)) if read_artifact else self._empty_manifest(),
            "note": "本工具只返回报告目录和链接，不返回正文。读取正文请调用 read_report_section(report_id, section_id, offset, max_chars)。",
        }

    def read_report_section(
        self,
        report_id: str,
        section_id: str,
        max_chars: int = 12000,
        offset: int = 0,
    ) -> dict:
        record = self.resolve_record(report_id)
        read_artifact = self._select_artifact(record, record.preferred_read)
        if read_artifact is None:
            raise FileNotFoundError(f"readable artifact not found: {report_id}")
        path = Path(read_artifact.internal_path)
        section = self._resolve_report_section(path, section_id)
        window = self._page_text(section["content"], max_chars=max_chars, offset=offset)
        return {
            "report": self._safe_record(record, include_artifacts=False),
            "section": {key: value for key, value in section.items() if key != "content"},
            "read_window": window,
        }

    def query_report(
        self,
        report_id: str,
        question: str = "",
        max_sections: int = 4,
        per_section_chars: int = 2600,
        total_chars: int = 9000,
    ) -> dict:
        """Return a bounded, question-focused evidence pack from a report.

        This is intentionally extractive. The LLM still writes the final answer,
        but it no longer needs to spend several tool calls discovering and paging
        through long reports.
        """
        record = self.resolve_record(report_id)
        read_artifact = self._select_artifact(record, record.preferred_read)
        if read_artifact is None:
            raise FileNotFoundError(f"readable artifact not found: {report_id}")
        path = Path(read_artifact.internal_path)
        sections = self._json_sections(path) if path.suffix.lower() == ".json" else self._markdown_sections(self._read_text_for_report(path))
        selected = self._select_report_sections(sections, question, max_sections=max_sections)
        budget = max(1000, min(int(total_chars or 9000), 12000))
        per_section_budget = max(500, min(int(per_section_chars or 2600), 4000))
        excerpts = []
        used = 0
        for section in selected:
            remaining = budget - used
            if remaining <= 0:
                break
            excerpt_budget = min(per_section_budget, remaining)
            excerpt = self._section_excerpt(section["content"], question, excerpt_budget)
            used += len(excerpt)
            excerpts.append(
                {
                    "section_id": section["section_id"],
                    "title": section["title"],
                    "level": section["level"],
                    "char_count": section["char_count"],
                    "excerpt_chars": len(excerpt),
                    "has_more": len(section["content"]) > len(excerpt),
                    "excerpt": excerpt,
                }
            )
        view_link = self._link_for_record(record)
        return {
            "report": self._safe_record(record, include_artifacts=False),
            "preferred_view_link": {
                "format": view_link["format"],
                "view_url": view_link["view_url"],
                "download_url": view_link["download_url"],
            },
            "question": question,
            "coverage": {
                "total_sections": len(sections),
                "selected_sections": len(excerpts),
                "returned_chars": sum(item["excerpt_chars"] for item in excerpts),
                "total_budget_chars": budget,
            },
            "sections": excerpts,
            "note": "这是按问题抽取的受控报告材料，不是全文。若用户要求精读某章，再调用 read_report_section。回答时引用 section_id/title，并说明未覆盖章节。"
        }

    def delete_report(self, report_id: str, permanent: bool = False) -> dict:
        record = self.resolve_record(report_id)
        paths = self._deletion_paths_for_record(record)
        if not paths:
            raise FileNotFoundError(f"no deletable files found for report: {report_id}")
        if permanent:
            deleted = []
            for path in paths:
                if path.is_dir():
                    shutil.rmtree(path)
                elif path.exists():
                    path.unlink()
                deleted.append(str(path))
            return {
                "status": "deleted",
                "mode": "permanent",
                "report": self._safe_record(record, include_artifacts=False),
                "deleted_paths": deleted,
            }

        trash_root = DATA_DIR / "report_trash" / datetime.now().strftime("%Y%m%d_%H%M%S")
        trash_root.mkdir(parents=True, exist_ok=True)
        moved = []
        for path in paths:
            target = trash_root / self._safe_trash_name(path)
            if path.is_dir():
                shutil.move(str(path), str(target))
            elif path.exists():
                shutil.move(str(path), str(target))
            moved.append({"from": str(path), "to": str(target)})
        manifest = {
            "report_id": report_id,
            "deleted_at": datetime.now().isoformat(timespec="seconds"),
            "mode": "trash",
            "report": self._safe_record(record, include_artifacts=True),
            "moved": moved,
        }
        (trash_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "status": "deleted",
            "mode": "trash",
            "trash_dir": str(trash_root),
            "report": self._safe_record(record, include_artifacts=False),
            "moved": moved,
        }

    def get_stock_research_status(self, ticker: str, stale_days: int = 60) -> dict:
        normalized = self._normalize_ticker_for_lookup(ticker)
        records = [
            item for item in self._catalog()
            if item.report_type == "stock_research" and item.subject.upper() == normalized
        ]
        if not records:
            return {
                "ticker": normalized,
                "research_status": "missing",
                "latest_report_id": None,
                "recommended_action": "run_initial_stock_research",
                "reason": "未发现本地个股深研报告。",
            }
        records.sort(key=lambda item: item.date, reverse=True)
        latest = records[0]
        days = self._days_since(latest.date)
        status = "stale" if days is not None and days > stale_days else "fresh"
        return {
            "ticker": normalized,
            "research_status": status,
            "latest_report_id": latest.report_id,
            "latest_stock_research_date": latest.date,
            "days_since_generated": days,
            "recommended_action": "reuse_existing_report" if status == "fresh" else "consider_refresh_stock_research",
            "report": self._safe_record(latest, include_artifacts=False),
        }

    def recommend_stock_research_action(self, ticker: str, stale_days: int = 60, major_event: bool = False) -> dict:
        status = self.get_stock_research_status(ticker, stale_days)
        if major_event:
            status["research_status"] = "invalidated"
            status["recommended_action"] = "refresh_stock_research"
            status["reason"] = "用户或系统标记存在重大变化，旧报告可能失效。"
        return status

    def _normalize_ticker_for_lookup(self, ticker: str) -> str:
        value = str(ticker or "").strip().upper()
        if _A_SHARE_CODE_RE.fullmatch(value):
            if value.endswith((".SH", ".SZ", ".BJ")):
                return value
            if value.startswith("6"):
                return f"{value}.SH"
            if value.startswith(("0", "3")):
                return f"{value}.SZ"
            if value.startswith(("4", "8")):
                return f"{value}.BJ"
        return value

    def list_reports(self, report_type: str | None = None, limit: int = 50) -> list[dict]:
        records = self.list_report_catalog(report_type=report_type, limit=limit)
        return records

    def get_latest_report(self, report_type: str | None = None) -> dict | None:
        items = self.list_report_catalog(report_type=report_type, limit=1)
        return items[0] if items else None

    def get_report(self, report_id: str, max_chars: int = 12000) -> dict:
        return self.get_report_detail(report_id, max_chars=max_chars)

    def get_report_view_link(self, report_id: str) -> dict:
        return self._link_for_record(self.resolve_record(report_id))

    def get_report_links(
        self,
        report_type: str | None = None,
        date: str | None = None,
        query: str | None = None,
        limit: int = 5,
    ) -> list[dict]:
        records = self._catalog()
        if report_type:
            records = [item for item in records if item.report_type == report_type]
        if date:
            records = [item for item in records if item.date == date]
        if query:
            selected = self._select_report_records_with_llm(records, query, limit)
            if selected:
                records = selected
        records.sort(key=lambda item: (item.date, item.title), reverse=True)
        return [self._link_for_record(item) for item in records[:limit]]

    def search_reports(self, query: str, limit: int = 20) -> list[dict]:
        return self.list_report_catalog(subject=query, limit=limit)

    def resolve_report(self, report_id: str) -> ReportMeta:
        record = self.resolve_record(report_id)
        artifact = self._select_artifact(record, record.preferred_view) or self._first_artifact(record)
        if artifact is None:
            raise FileNotFoundError(f"report artifact not found: {report_id}")
        return ReportMeta(
            report_id=record.report_id,
            report_type=record.report_type,
            title=record.title,
            date=record.date,
            format=artifact.format,
            internal_path=artifact.internal_path,
        )

    def resolve_record(self, report_id: str) -> ReportRecord:
        for record in self._catalog():
            if record.report_id == report_id:
                return record
        raise FileNotFoundError(f"report not found: {report_id}")

    def _deletion_paths_for_record(self, record: ReportRecord) -> list[Path]:
        artifact_paths = [Path(item.internal_path).resolve() for item in record.artifacts.values()]
        allowed_roots = [BETTAFISH_MARKET_REPORT_ROOT.resolve(), TRADING_REPORT_ROOT.resolve(), TRADING_REPORT_ROOT.parent.resolve()]
        for path in artifact_paths:
            self._assert_deletable_path(path, allowed_roots)
        if record.source == "TradingAgents":
            parents = {path.parent.resolve() for path in artifact_paths}
            if len(parents) == 1:
                parent = next(iter(parents))
                self._assert_deletable_path(parent, allowed_roots)
                if parent.name != "final_reports":
                    return [parent]
        return sorted(set(artifact_paths), key=lambda item: str(item))

    def _assert_deletable_path(self, path: Path, allowed_roots: list[Path]) -> None:
        if not path.exists():
            return
        resolved = path.resolve()
        if not any(resolved == root or root in resolved.parents for root in allowed_roots):
            raise PermissionError(f"report path outside allowed roots: {resolved}")
        if resolved in allowed_roots:
            raise PermissionError(f"refuse to delete report root: {resolved}")

    def _safe_trash_name(self, path: Path) -> str:
        name = path.name or "report"
        return re.sub(r"[^A-Za-z0-9._\\-\\u4e00-\\u9fff]+", "_", name)[:160]

    def _catalog(self) -> list[ReportRecord]:
        return self._scan_bettafish_catalog() + self._scan_tradingagents_catalog()

    def _scan_bettafish_catalog(self) -> list[ReportRecord]:
        root = BETTAFISH_MARKET_REPORT_ROOT
        if not root.exists():
            return []
        records: list[ReportRecord] = []
        for skill_name, date_dir in self._iter_bettafish_date_dirs(root):
            metadata = self._load_metadata(date_dir / "metadata.json")
            report_type = metadata.get("report_type") or skill_name
            category = metadata.get("category") or self._category_for_type(report_type)
            subject = metadata.get("subject") or ("A股全市场" if report_type == "market_discovery" else skill_name)
            title = metadata.get("title") or self._default_title(report_type, subject, date_dir.name)
            tags = metadata.get("tags") or [subject]
            artifacts = self._collect_bettafish_artifacts(date_dir, skill_name)
            if "html" not in artifacts:
                continue
            report_id = f"{report_type}:{subject}:{date_dir.name}".replace(" ", "_")
            if "html" not in artifacts:
                continue
            records.append(
                ReportRecord(
                    report_id=report_id,
                    source="BettaFish",
                    report_type=report_type,
                    category=category,
                    subject=subject,
                    date=date_dir.name,
                    title=title,
                    tags=tags,
                    preferred_view="html" if "html" in artifacts else "md",
                    preferred_read="md" if "md" in artifacts else ("json" if "json" in artifacts else self._first_format(artifacts)),
                    artifacts=artifacts,
                )
            )
        return records

    def _iter_bettafish_date_dirs(self, root: Path) -> list[tuple[str, Path]]:
        rows: list[tuple[str, Path]] = []
        seen: set[Path] = set()
        default_skill = root.name if root.name else "market_discovery"
        for item in root.iterdir():
            if not item.is_dir():
                continue
            if self._looks_like_date(item.name):
                resolved = item.resolve()
                if resolved not in seen:
                    rows.append((default_skill, item))
                    seen.add(resolved)
                continue
            for date_dir in item.iterdir():
                if date_dir.is_dir() and self._looks_like_date(date_dir.name):
                    resolved = date_dir.resolve()
                    if resolved not in seen:
                        rows.append((item.name, date_dir))
                        seen.add(resolved)
        return rows

    def _scan_tradingagents_catalog(self) -> list[ReportRecord]:
        roots = [
            TRADING_REPORT_ROOT,
            TRADING_REPORT_ROOT / "reports",
            TRADING_REPORT_ROOT.parent / "results",
        ]
        records: list[ReportRecord] = []
        candidates: set[Path] = set()
        for root in roots:
            if root.exists():
                candidates.update(root.rglob("complete_report.md"))
        parent = TRADING_REPORT_ROOT.parent
        if parent.exists():
            candidates.update(parent.rglob("TradingAgentsStrategy_reports/*/complete_report.md"))

        for md_path in candidates:
            date_value = self._extract_date(md_path)
            ticker = self._extract_ticker(md_path)
            metadata = self._load_metadata(md_path.parent / "metadata.json")
            subject = metadata.get("subject") or ticker or "unknown"
            title = metadata.get("title") or f"{subject} 个股深度研究报告"
            artifacts = {
                "md": ReportArtifact("md", str(md_path)),
            }
            final_state = md_path.parent / "final_state.json"
            if final_state.exists():
                artifacts["json"] = ReportArtifact("json", str(final_state))
            html_files = sorted(md_path.parent.glob("*.html"), key=lambda item: item.stat().st_mtime, reverse=True)
            html_files.extend(
                sorted(
                    (md_path.parent / "report_engine" / "final_reports").glob("final_report_*.html"),
                    key=lambda item: item.stat().st_mtime,
                    reverse=True,
                )
            )
            if html_files:
                artifacts["html"] = ReportArtifact("html", str(html_files[0]))
            report_id = f"stock_research:{subject}:{date_value}"
            days = self._days_since(date_value)
            records.append(
                ReportRecord(
                    report_id=report_id,
                    source="TradingAgents",
                    report_type="stock_research",
                    category="个股层",
                    subject=subject,
                    date=date_value,
                    title=title,
                    tags=metadata.get("tags") or [subject, "个股研究"],
                    preferred_view="html" if "html" in artifacts else "md",
                    preferred_read="md",
                    artifacts=artifacts,
                    freshness="stale" if days is not None and days > 60 else "fresh",
                    days_since_generated=days,
                    recommended_action="reuse_existing_report" if days is not None and days <= 60 else "consider_refresh_stock_research",
                )
            )
        return records

    def _collect_bettafish_artifacts(self, date_dir: Path, skill_name: str) -> dict[str, ReportArtifact]:
        artifacts: dict[str, ReportArtifact] = {}
        html_files = sorted((date_dir / "report_engine" / "final_reports").glob("final_report_*.html"), key=lambda item: item.stat().st_mtime, reverse=True)
        if html_files:
            artifacts["html"] = ReportArtifact("html", str(html_files[0]))
        md_candidates = [date_dir / f"{skill_name}.md", date_dir / f"{skill_name.replace('-', '_')}.md"]
        md_candidates.extend(sorted(date_dir.glob("*.md"), key=lambda item: item.stat().st_mtime, reverse=True))
        for item in md_candidates:
            if item.exists() and item.name not in {"forum_logs.md"}:
                artifacts["md"] = ReportArtifact("md", str(item))
                break
        json_candidates = sorted(date_dir.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
        for item in json_candidates:
            if item.name not in {"engine_reports.json"}:
                artifacts["json"] = ReportArtifact("json", str(item))
                break
        return artifacts

    def _link_for_record(self, record: ReportRecord, format: str | None = None) -> dict:
        selected = self._select_artifact(record, format or record.preferred_view) or self._first_artifact(record)
        if selected is None:
            raise FileNotFoundError(f"report artifact not found: {record.report_id}")
        encoded_id = quote(record.report_id, safe="")
        suffix = f"?format={quote(selected.format, safe='')}"
        return {
            "report": self._safe_record(record, include_artifacts=False),
            "meta": self._safe_record(record, include_artifacts=False),
            "format": selected.format,
            "view_url": f"{FINCLAW_API_BASE_URL}/api/reports/{encoded_id}/view{suffix}",
            "download_url": f"{FINCLAW_API_BASE_URL}/api/reports/{encoded_id}/download{suffix}",
        }

    def _safe_record(self, record: ReportRecord, include_artifacts: bool) -> dict:
        data = asdict(record)
        data["display_source"] = self._display_source(record.source)
        if include_artifacts:
            data["artifacts"] = {
                key: {"format": value.format, "available": True}
                for key, value in record.artifacts.items()
            }
        else:
            data.pop("artifacts", None)
            data["available_formats"] = sorted(record.artifacts)
        return data

    def _normalize_source_filter(self, source: str) -> str:
        value = str(source or "").strip().lower()
        if value in {"个股深研", "equityscope", "equity scope", "单标的深度研究", "个股研究"}:
            return "TradingAgents"
        if value in {"主线雷达", "themeradar", "theme radar", "市场主线研究", "题材发现"}:
            return "BettaFish"
        return source

    def _display_source(self, source: str) -> str:
        if source == "TradingAgents":
            return "个股深研 / EquityScope"
        if source == "BettaFish":
            return "主线雷达 / ThemeRadar"
        return source

    def _select_artifact(self, record: ReportRecord, format: str | None) -> ReportArtifact | None:
        if not format:
            return None
        return record.artifacts.get(format.lower())

    def _first_artifact(self, record: ReportRecord) -> ReportArtifact | None:
        for key in ("html", "md", "json"):
            if key in record.artifacts:
                return record.artifacts[key]
        return next(iter(record.artifacts.values()), None)

    def _first_format(self, artifacts: dict[str, ReportArtifact]) -> str:
        for key in ("html", "md", "json"):
            if key in artifacts:
                return key
        return next(iter(artifacts))

    def _read_text_for_report(self, path: Path) -> str:
        if path.suffix.lower() == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
            return json.dumps(data, ensure_ascii=False, indent=2)
        return path.read_text(encoding="utf-8", errors="ignore")

    def _page_text(self, text: str, max_chars: int, offset: int = 0) -> dict:
        offset = max(0, int(offset or 0))
        max_chars = max(1, min(int(max_chars or 12000), 12000))
        end = min(offset + max_chars, len(text))
        return {
            "content": text[offset:end],
            "offset": offset,
            "max_chars": max_chars,
            "total_chars": len(text),
            "has_more": end < len(text),
            "next_offset": end if end < len(text) else None,
        }

    def _select_report_sections(self, sections: list[dict], question: str, max_sections: int) -> list[dict]:
        max_sections = max(1, min(int(max_sections or 4), 8))
        selected_ids = self._select_report_section_ids_with_llm(sections, question, max_sections)
        if selected_ids:
            section_map = {str(section.get("section_id")): section for section in sections}
            selected = [section_map[section_id] for section_id in selected_ids if section_id in section_map]
            if selected:
                selected.sort(key=lambda item: int(str(item.get("section_id") or "s999").lstrip("s") or 999))
                return selected[:max_sections]
        selected = sections[:max_sections]
        selected.sort(key=lambda item: int(str(item.get("section_id") or "s999").lstrip("s") or 999))
        return selected

    def _section_excerpt(self, content: str, question: str, max_chars: int) -> str:
        text = str(content or "").strip()
        if len(text) <= max_chars:
            return text
        return text[:max_chars].rstrip()

    def _select_report_records_with_llm(self, records: list[ReportRecord], subject: str, limit: int) -> list[ReportRecord]:
        if not llm_client.configured or not records:
            return []
        bounded = records[:120]
        payload = {
            "subject": subject,
            "records": [
                {
                    "record_id": item.report_id,
                    "source": item.source,
                    "report_type": item.report_type,
                    "subject": item.subject,
                    "date": item.date,
                    "title": item.title,
                    "tags": item.tags,
                    "freshness": item.freshness,
                }
                for item in bounded
            ],
            "max_selected": max(1, min(int(limit or 50), 50)),
        }
        prompt = (
            "你是报告目录语义选择器。根据 subject 选择真正相关的报告。"
            "不要做关键词匹配；按标的、主线、上下游关系、报告类型、时间和标题语义判断。"
            "只返回严格 JSON：{\"selected_report_ids\":[\"...\"]}。没有相关报告则返回空数组。"
        )
        try:
            parsed = llm_client.chat_json(
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
                ],
                purpose="report_catalog_selector",
            )
        except Exception:
            return []
        selected_ids = parsed.get("selected_report_ids") if isinstance(parsed, dict) else None
        if not isinstance(selected_ids, list):
            return []
        order = [str(item) for item in selected_ids]
        record_map = {item.report_id: item for item in bounded}
        return [record_map[report_id] for report_id in order if report_id in record_map]

    def _select_report_section_ids_with_llm(self, sections: list[dict], question: str, max_sections: int) -> list[str]:
        if not llm_client.configured or not sections:
            return []
        manifest = [
            {
                "section_id": section.get("section_id"),
                "title": section.get("title"),
                "level": section.get("level"),
                "char_count": section.get("char_count"),
                "preview": str(section.get("content") or "")[:360],
            }
            for section in sections[:120]
        ]
        prompt = (
            "你是报告章节导航器。根据问题选择最值得读取的章节。"
            "不要做关键词匹配；按问题意图、章节标题、预览语义和信息覆盖判断。"
            "只返回严格 JSON：{\"section_ids\":[\"s001\"]}。最多返回 max_sections 个。"
        )
        try:
            parsed = llm_client.chat_json(
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps({"question": question, "max_sections": max_sections, "sections": manifest}, ensure_ascii=False, default=str)},
                ],
                purpose="report_section_selector",
            )
        except Exception:
            return []
        section_ids = parsed.get("section_ids") if isinstance(parsed, dict) else None
        if not isinstance(section_ids, list):
            return []
        return [str(item) for item in section_ids[:max_sections]]

    def _report_manifest(self, path: Path) -> dict:
        if path.suffix.lower() == ".json":
            return self._json_manifest(path)
        text = self._read_text_for_report(path)
        sections = self._markdown_sections(text)
        return {
            "format": path.suffix.lower().lstrip(".") or "text",
            "total_chars": len(text),
            "sections": [
                {key: value for key, value in section.items() if key != "content"}
                for section in sections
            ],
        }

    def _resolve_report_section(self, path: Path, section_id: str) -> dict:
        section_id = str(section_id or "").strip()
        if not section_id:
            raise ValueError("section_id is required")
        sections = self._json_sections(path) if path.suffix.lower() == ".json" else self._markdown_sections(self._read_text_for_report(path))
        for section in sections:
            if section["section_id"] == section_id or section["title"] == section_id:
                return section
        raise KeyError(f"section not found: {section_id}")

    def _markdown_sections(self, text: str) -> list[dict]:
        matches = list(re.finditer(r"^(#{1,6})\s+(.+?)\s*$", text, flags=re.MULTILINE))
        if not matches:
            return [
                {
                    "section_id": "s001",
                    "title": "全文",
                    "level": 1,
                    "start": 0,
                    "end": len(text),
                    "char_count": len(text),
                    "content": text,
                }
            ]
        sections: list[dict] = []
        for idx, match in enumerate(matches):
            start = match.start()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
            content = text[start:end].strip()
            sections.append(
                {
                    "section_id": f"s{idx + 1:03d}",
                    "title": match.group(2).strip(),
                    "level": len(match.group(1)),
                    "start": start,
                    "end": end,
                    "char_count": len(content),
                    "content": content,
                }
            )
        return sections

    def _json_manifest(self, path: Path) -> dict:
        data = json.loads(path.read_text(encoding="utf-8"))
        sections = self._json_sections(path, data=data)
        return {
            "format": "json",
            "total_chars": len(json.dumps(data, ensure_ascii=False, indent=2)),
            "sections": [
                {key: value for key, value in section.items() if key != "content"}
                for section in sections
            ],
        }

    def _json_sections(self, path: Path, data: object | None = None) -> list[dict]:
        if data is None:
            data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            rows = list(data.items())
        else:
            rows = [("root", data)]
        sections = []
        for idx, (key, value) in enumerate(rows):
            content = json.dumps(value, ensure_ascii=False, indent=2, default=str)
            sections.append(
                {
                    "section_id": f"s{idx + 1:03d}",
                    "title": str(key),
                    "level": 1,
                    "start": 0,
                    "end": len(content),
                    "char_count": len(content),
                    "content": content,
                }
            )
        return sections

    @staticmethod
    def _empty_manifest() -> dict:
        return {"format": None, "total_chars": 0, "sections": []}

    @staticmethod
    def _empty_content_window() -> dict:
        return {
            "content": "",
            "offset": 0,
            "max_chars": 0,
            "total_chars": 0,
            "has_more": False,
            "next_offset": None,
        }

    def _load_metadata(self, path: Path) -> dict:
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _extract_date(self, path: Path) -> str:
        for part in path.parts:
            if self._looks_like_date(part):
                return part
        return "unknown-date"

    def _extract_ticker(self, path: Path) -> str | None:
        for part in reversed(path.parts):
            upper = part.upper()
            if len(upper) == 9 and upper[6] == "." and upper[:6].isdigit():
                return upper
            if len(upper) == 6 and upper.isdigit():
                return f"{upper}.SH" if upper.startswith("6") else f"{upper}.SZ"
        return None

    def _looks_like_date(self, value: str) -> bool:
        return len(value) == 10 and value[4] == "-" and value[7] == "-"

    def _days_since(self, value: str) -> int | None:
        try:
            parsed = date_cls.fromisoformat(value)
        except ValueError:
            return None
        return (date_cls.today() - parsed).days

    def _category_for_type(self, report_type: str) -> str:
        if report_type in {"market_discovery", "market_context", "market_review"}:
            return "市场层"
        if report_type in {"theme_deep_dive", "theme_compare", "theme_heat"}:
            return "主题/板块层"
        if report_type in {"stock_research", "stock_compare", "stock_heat", "watchlist_review"}:
            return "个股层"
        if report_type in {"pre_market_plan", "intraday_event_review", "post_market_review"}:
            return "交易计划层"
        return "其他"

    def _default_title(self, report_type: str, subject: str, date: str) -> str:
        labels = {
            "market_discovery": "A股市场主线发现报告",
            "theme_deep_dive": "主线深度调研报告",
            "theme_compare": "主线对比报告",
            "stock_research": "个股深度研究报告",
        }
        return f"{date} {subject} {labels.get(report_type, report_type)}"


report_library = ReportLibrary()
