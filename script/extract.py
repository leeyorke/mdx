import sqlite3
from bs4 import BeautifulSoup
import json


def parse_dictionary_entry(entry_html):
    """解析字典条目 HTML，返回结构化数据"""
    soup = BeautifulSoup(entry_html, "html.parser")
    entry_data = {}

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
    phonetics = {}
    uk_phon = soup.find("div", class_="phons_br")
    if uk_phon:
        phonetics["uk"] = uk_phon.find("span", class_="phon").get_text(strip=True)

    us_phon = soup.find("div", class_="phons_n_am")
    if us_phon:
        phonetics["us"] = us_phon.find("span", class_="phon").get_text(strip=True)

    if phonetics:
        entry_data["phonetics"] = phonetics

    # 提取释义信息
    senses = []
    for sense in soup.find_all("li", class_="sense"):
        sense_data = {}

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
        examples = []
        for example in sense.find_all("span", class_="x"):
            # 创建副本避免修改原始 soup
            example_copy = BeautifulSoup(str(example), "html.parser").find(
                "span", class_="x"
            )

            # 提取中文部分
            xt_tag = example_copy.find("xt")
            chn_tag = xt_tag.find("chn") if xt_tag else None
            chn_text = chn_tag.get_text(strip=True) if chn_tag else ""

            # 移除 xt 标签，提取英文部分
            if xt_tag:
                xt_tag.decompose()

            # 提取英文例句：保留结构中的空格
            ex_text_parts = []
            for child in example_copy.find_all(recursive=False):
                if child.name == "span" and "cl" in child.get("class", []):
                    ex_text_parts.append(child.get_text(strip=True))
                elif child.name is None and child.strip():
                    ex_text_parts.append(child.strip())

            ex_text = " ".join(ex_text_parts).strip()

            examples.append({"text": ex_text, "chinese": chn_text})

        if examples:
            sense_data["examples"] = examples

        # 主题标签
        topics = []
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


def query_single_word(word):
    # 连接到 SQLite 数据库（请根据实际情况修改）
    conn = sqlite3.connect("oaldpe.db")
    cursor = conn.cursor()

    # 查询单个条目（参数化查询）
    cursor.execute("SELECT entry, paraphrase FROM mdx WHERE entry = ?", (word,))
    result = cursor.fetchone()

    conn.close()

    if result and result[1] and "<" in result[1]:
        return parse_dictionary_entry(result[1])
    return None


if __name__ == "__main__":
    # 查询指定单词
    word = "track"  # 可修改为需要查询的单词
    parsed_data = query_single_word(word)

    # 输出结果
    if parsed_data:
        print(json.dumps(parsed_data, ensure_ascii=False, indent=2))
    else:
        print(f"未找到单词 '{word}' 的有效释义")
