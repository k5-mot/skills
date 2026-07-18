"""Prepare collected technology news findings for an Agent-side Japanese report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Any


def format_news_report(
    items: list[dict[str, Any]] | None = None,
    articles: list[dict[str, Any]] | None = None,
    podcasts: list[dict[str, Any]] | None = None,
    title: str = "技術ニュースレポート",
    focus: str = "生成AI、クラウド、FinOps、開発ツール",
) -> dict[str, Any]:
    """Normalize already-collected news items for the calling Agent.

    News collection itself should be done with RSS/search tools so citations
    and source freshness remain visible to the user. The final Japanese
    Markdown report should be written by the Agent that invoked the Skill,
    using the returned ``report_input``.
    """
    try:
        items = items or []
        articles = articles or []
        podcasts = podcasts or []
        report_date = datetime.now(timezone.utc).date().isoformat()

        news_input = []
        for item in items:
            news_input.append(
                {
                    "title": str(item.get("title") or item.get("headline") or "タイトル未設定"),
                    "category": str(item.get("category") or item.get("genre") or "トピック"),
                    "source_category": str(item.get("source_category") or item.get("source_genre") or ""),
                    "source": str(item.get("source") or item.get("site") or "出典未設定"),
                    "url": str(item.get("url") or ""),
                    "published_at": str(item.get("published_at") or item.get("date") or item.get("published") or ""),
                    "raw_summary": str(item.get("summary") or item.get("description") or item.get("content") or ""),
                    "impact_hint": str(item.get("impact") or item.get("importance") or ""),
                }
            )

        article_input = []
        for article in articles:
            article_input.append(
                {
                    "title": str(article.get("title") or article.get("headline") or "タイトル未設定"),
                    "category": str(article.get("category") or article.get("genre") or "技術記事"),
                    "source_category": str(article.get("source_category") or article.get("source_genre") or ""),
                    "source": str(article.get("source") or article.get("site") or "出典未設定"),
                    "url": str(article.get("url") or ""),
                    "published_at": str(article.get("published_at") or article.get("date") or article.get("published") or ""),
                    "raw_summary": str(article.get("summary") or article.get("description") or article.get("content") or ""),
                    "audience_hint": str(article.get("recommended_for") or article.get("audience") or ""),
                    "learning_hint": str(article.get("takeaways") or article.get("learning_points") or article.get("impact") or ""),
                    "use_case_hint": str(article.get("use_case") or article.get("application") or ""),
                }
            )

        podcast_input = []
        for podcast in podcasts:
            source = str(podcast.get("source") or podcast.get("show") or podcast.get("program") or "番組未設定")
            podcast_input.append(
                {
                    "title": str(podcast.get("title") or podcast.get("episode_title") or "タイトル未設定"),
                    "category": str(podcast.get("category") or podcast.get("genre") or "Podcast"),
                    "source_category": str(podcast.get("source_category") or podcast.get("source_genre") or ""),
                    "source": source,
                    "url": str(podcast.get("url") or ""),
                    "published_at": str(podcast.get("published_at") or podcast.get("date") or podcast.get("published") or ""),
                    "episode": str(podcast.get("episode") or podcast.get("episode_number") or source),
                    "raw_summary": str(podcast.get("summary") or podcast.get("description") or podcast.get("content") or ""),
                    "highlight_hint": str(podcast.get("highlights") or podcast.get("listening_points") or podcast.get("impact") or ""),
                    "audience_hint": str(podcast.get("recommended_for") or podcast.get("audience") or ""),
                }
            )

        lines = [
            f"# {title} source inventory ({report_date})",
            f"- focus: {focus}",
            f"- news: {len(news_input)}",
            f"- articles: {len(article_input)}",
            f"- podcasts: {len(podcast_input)}",
        ]
        if news_input:
            lines.append("\n## news")
            for index, item in enumerate(news_input, start=1):
                lines.append(f"- {index}. {item['category']}: {item['title']} - {item['source']} ({item['url'] or 'N/A'})")
        if article_input:
            lines.append("\n## articles")
            for index, item in enumerate(article_input, start=1):
                lines.append(f"- {index}. {item['category']}: {item['title']} - {item['source']} ({item['url'] or 'N/A'})")
        if podcast_input:
            lines.append("\n## podcasts")
            for index, item in enumerate(podcast_input, start=1):
                lines.append(f"- {index}. {item['category']}: {item['title']} - {item['source']} ({item['url'] or 'N/A'})")

        report_input = {
            "title": title,
            "date": report_date,
            "focus": focus,
            "news": news_input,
            "articles": article_input,
            "podcasts": podcast_input,
        }
        metadata = {"items": len(news_input), "articles": len(article_input), "podcasts": len(podcast_input), "focus": focus}
        return {"ok": True, "source_inventory_markdown": "\n".join(lines), "report_input": report_input, "metadata": metadata}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "exception_type": exc.__class__.__name__}


def _build_parser() -> argparse.ArgumentParser:
    """Build the technology news report helper CLI parser."""
    parser = argparse.ArgumentParser(description="JSON項目を技術ニュースレポート用の素材JSONに正規化します。")
    parser.add_argument("--items-json", help="JSON array of news items")
    parser.add_argument("--items-file", help="Path to a JSON file containing an array of news items")
    parser.add_argument("--articles-json", help="JSON array of recommended technical articles")
    parser.add_argument("--articles-file", help="Path to a JSON file containing recommended technical articles")
    parser.add_argument("--podcasts-json", help="JSON array of recommended podcast episodes")
    parser.add_argument("--podcasts-file", help="Path to a JSON file containing recommended podcast episodes")
    parser.add_argument("--title", default="技術ニュースレポート")
    parser.add_argument("--focus", default="生成AI、クラウド、FinOps、開発ツール")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the technology news report helper CLI."""
    args = _build_parser().parse_args(argv)
    if args.items_file:
        items = json.loads(Path(args.items_file).read_text(encoding="utf-8"))
    else:
        items = json.loads(args.items_json) if args.items_json else []
    if args.articles_file:
        articles = json.loads(Path(args.articles_file).read_text(encoding="utf-8"))
    else:
        articles = json.loads(args.articles_json) if args.articles_json else []
    if args.podcasts_file:
        podcasts = json.loads(Path(args.podcasts_file).read_text(encoding="utf-8"))
    else:
        podcasts = json.loads(args.podcasts_json) if args.podcasts_json else []
    result = format_news_report(items=items, articles=articles, podcasts=podcasts, title=args.title, focus=args.focus)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
