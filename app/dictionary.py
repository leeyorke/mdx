"""字典查询模块 - 封装查询和解析逻辑"""

import sqlite3
from bs4 import BeautifulSoup
from pathlib import Path


def _get_db_path() -> str:
    """获取数据库文件的路径（相对于项目根目录）"""
    return str(Path(__file__).resolve().parent.parent / "oaldpe.db")


def parse_dictionary_entry(entry_html: str) -> dict:
    """解析字典条目 HTML，返回结构化数据"""
    soup = BeautifulSoup(entry_html, "html.parser")
    entry_data: dict = {}

    # 提取头部信息
    headword = soup.find("h1", class_="headword")
    if headword:
        entry_data["headword"] = headword.get_text(strip=True)
        entry_data["id"] = headword.get("id", "")

    # 提取词性
    pos = soup.find("span", class_="pos")
    if pos:
        entry_data["pos"] = pos.get_text(strip=True)

    # 提取发音信息
    phonetics: dict = {}
    uk_phon = soup.find("div", class_="phons_br")
    if uk_phon:
        phone_span = uk_phon.find("span", class_="phon")
        if phone_span:
            phonetics["uk"] = phone_span.get_text(strip=True)

    us_phon = soup.find("div", class_="phons_n_am")
    if us_phon:
        phone_span = us_phon.find("span", class_="phon")
        if phone_span:
            phonetics["us"] = phone_span.get_text(strip=True)

    if phonetics:
        entry_data["phonetics"] = phonetics

    # 提取释义信息
    senses: list[dict] = []
    for sense in soup.find_all("li", class_="sense"):
        sense_data: dict = {}

        # 释义编号
        sense_number = sense.find("span", class_="iteration")
        if sense_number:
            sense_data["number"] = sense_number.get_text(strip=True)

        # 英文定义
        definition = sense.find("span", class_="def")
        if definition:
            sense_data["definition"] = definition.get_text(strip=True)

        # 中文定义
        chn_def = sense.find("deft")
        if chn_def:
            chn_text = chn_def.find("chn")
            if chn_text:
                sense_data["chinese_definition"] = chn_text.get_text(strip=True)

        # 例句
        examples: list[dict] = []
        for example in sense.find_all("span", class_="x"):
            example_copy = BeautifulSoup(str(example), "html.parser").find(
                "span", class_="x"
            )

            # 提取中文部分
            xt_tag = example_copy.find("xt") if example_copy else None
            chn_tag = xt_tag.find("chn") if xt_tag else None
            chn_text = chn_tag.get_text(strip=True) if chn_tag else ""

            # 移除 xt 标签，提取英文部分
            if xt_tag:
                xt_tag.decompose()

            ex_text_parts: list[str] = []
            for child in (example_copy or []).find_all(recursive=False):
                if child.name == "span" and "cl" in child.get("class", []):
                    ex_text_parts.append(child.get_text(strip=True))
                elif child.name is None and child.strip():
                    ex_text_parts.append(child.strip())

            ex_text = " ".join(ex_text_parts).strip()

            examples.append({"text": ex_text, "chinese": chn_text})

        if examples:
            sense_data["examples"] = examples

        # 主题标签
        topics: list[dict] = []
        for topic in sense.find_all("a", class_="topic"):
            topic_name = topic.find("span", class_="topic_name")
            topic_level = topic.find("span", class_="topic_cefr")
            if topic_name and topic_level:
                topics.append(
                    {
                        "name": topic_name.get_text(strip=True),
                        "level": topic_level.get_text(strip=True),
                    }
                )

        if topics:
            sense_data["topics"] = topics

        senses.append(sense_data)

    if senses:
        entry_data["senses"] = senses

    return entry_data


def query_word(word: str) -> dict | None:
    """查询单词释义，返回结构化数据；未找到返回 None"""
    conn = sqlite3.connect(_get_db_path())
    cursor = conn.cursor()
    cursor.execute("SELECT entry, paraphrase FROM mdx WHERE entry = ?", (word,))
    result = cursor.fetchone()
    conn.close()

    if result and result[1] and "<" in result[1]:
        return parse_dictionary_entry(result[1])
    return None


def search_words(query: str, limit: int = 20) -> list[str]:
    """模糊搜索单词（LIKE 匹配），返回匹配的单词列表"""
    conn = sqlite3.connect(_get_db_path())
    cursor = conn.cursor()
    cursor.execute(
        "SELECT entry FROM mdx WHERE entry LIKE ? LIMIT ?",
        (f"{query}%", limit),
    )
    results = [row[0] for row in cursor.fetchall()]
    conn.close()
    return results
