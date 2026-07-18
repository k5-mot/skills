import importlib.util
from pathlib import Path


def load_report_module():
    """テスト対象の report モジュールを読み込む。"""
    path = Path(__file__).resolve().parents[1] / "scripts" / "report.py"
    spec = importlib.util.spec_from_file_location("tech_news_module_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_format_news_report_prepares_report_input():
    """技術ニュース素材がレポート入力へ正規化されることを検証する。"""
    report = load_report_module()

    result = report.format_news_report(
        items=[
            {
                "title": "Cloud cost tool update",
                "source_category": "公式ベンダー・クラウド / AI",
                "source": "Vendor blog",
                "url": "https://example.test/news",
                "summary": "A FinOps workflow was improved.",
                "impact": "Review cost dashboards.",
            }
        ],
        articles=[
            {
                "title": "Practical platform engineering",
                "category": "Developer Tools",
                "source": "Engineering Blog",
                "url": "https://example.test/article",
                "summary": "A practical article about platform workflows.",
                "recommended_for": "Platform engineers",
                "takeaways": "Improve internal developer experience.",
                "use_case": "Apply the workflow to service templates.",
            }
        ],
        podcasts=[
            {
                "title": "AI infra roundtable",
                "category": "生成AI",
                "source": "Tech Podcast",
                "url": "https://example.test/podcast",
                "episode": "Episode 42",
                "summary": "A discussion about AI infrastructure.",
                "highlights": "Model serving and GPU operations.",
                "recommended_for": "AI platform teams",
            }
        ],
    )

    assert result["ok"] is True
    assert "content_markdown" not in result
    assert "source_inventory_markdown" in result
    assert "Cloud cost tool update - Vendor blog" in result["source_inventory_markdown"]
    report_input = result["report_input"]
    assert report_input["news"][0]["title"] == "Cloud cost tool update"
    assert report_input["news"][0]["source"] == "Vendor blog"
    assert report_input["news"][0]["url"] == "https://example.test/news"
    assert report_input["news"][0]["raw_summary"] == "A FinOps workflow was improved."
    assert report_input["news"][0]["impact_hint"] == "Review cost dashboards."
    assert report_input["articles"][0]["category"] == "Developer Tools"
    assert report_input["articles"][0]["learning_hint"] == "Improve internal developer experience."
    assert report_input["articles"][0]["use_case_hint"] == "Apply the workflow to service templates."
    assert report_input["podcasts"][0]["category"] == "生成AI"
    assert report_input["podcasts"][0]["highlight_hint"] == "Model serving and GPU operations."
    assert result["metadata"]["articles"] == 1
    assert result["metadata"]["podcasts"] == 1
