import yaml
import bibtexparser
from jinja2 import Environment, FileSystemLoader
import os
from collections import defaultdict

def load_data():
    # 1. 读取配置文件 (包含 info, bio, education, activities)
    with open('data/config.yaml', 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    
    # 2. 读取评级字典
    with open('data/rankings.yaml', 'r', encoding='utf-8') as f:
        rankings = yaml.safe_load(f)

    # 3. 读取正式发表 BibTeX
    with open('data/papers.bib', 'r', encoding='utf-8') as f:
        bib_db = bibtexparser.load(f)
        
    # 4. 读取待发表 BibTeX (如果文件不存在则为空列表)
    preprints_entries = []
    if os.path.exists('data/preprints.bib'):
        with open('data/preprints.bib', 'r', encoding='utf-8') as f:
            preprints_db = bibtexparser.load(f)
            preprints_entries = preprints_db.entries
    
    return config, rankings, bib_db.entries, preprints_entries

def clean_and_tag_paper(p, rankings):
    """
    辅助函数：清洗单个论文数据并打标签
    """
    # 获取期刊/会议名称
    venue_name = p.get('journal') or p.get('booktitle') or "Preprint / Under Review"
    p['venue'] = venue_name
    
    # 清理标题和作者
    p['title'] = p.get('title', '').replace('{', '').replace('}', '')
    p['author'] = p.get('author', '').replace('and', ',').replace('\n', ' ')
    
    # 匹配评级
    p['tags'] = []
    # 默认为灰色，如果是待发表可能用不同颜色，这里保持逻辑一致
    
    for k, v in rankings.items():
        if k.lower() in venue_name.lower():
            p['tags'] = v['tags']
            break
            
    return p

def process_all_papers(published_raw, preprints_raw, rankings):
    # 初始化统计数据
    stats = defaultdict(int)
    
    # 1. 处理正式发表论文
    published_papers = []
    for p in published_raw:
        p = clean_and_tag_paper(p, rankings)
        published_papers.append(p)
        
        # 仅对正式发表的进行统计
        stats['total'] += 1
        for tag in p['tags']:
            if "CCF" in tag: stats['ccf_total'] += 1
            if "CCF A" in tag: stats['ccf_a'] += 1
            if "CCF B" in tag: stats['ccf_b'] += 1
            if "JCR Q1" in tag: stats['jcr_q1'] += 1
            if "JCR Q2" in tag: stats['jcr_q2'] += 1

    # 2. 处理待发表论文 (不计入 stats)
    preprint_papers = []
    for p in preprints_raw:
        p = clean_and_tag_paper(p, rankings)
        # 可以在这里强制加一个 tag
        if not p['tags']:
            p['tags'] = ['Preprint']
        preprint_papers.append(p)
    
    # 可选：按年份排序
    published_papers.sort(key=lambda x: x.get('year', '0000'), reverse=True)
    preprint_papers.sort(key=lambda x: x.get('year', '0000'), reverse=True)

    return published_papers, preprint_papers, stats

def build():
    config, rankings, papers_raw, preprints_raw = load_data()
    
    # 处理两类论文
    papers, preprints, stats = process_all_papers(papers_raw, preprints_raw, rankings)
    
    # 注入数据
    config['stats'] = stats
    
    env = Environment(loader=FileSystemLoader('.'))
    template = env.get_template('template.html')
    
    output_html = template.render(
        info=config['info'],
        bio=config['bio'],
        edu=config['education'],
        activities=config.get('activities', []), # 新增
        papers=papers,
        preprints=preprints, # 新增
        stats=stats
    )
    
    os.makedirs('output', exist_ok=True)
    with open('output/index.html', 'w', encoding='utf-8') as f:
        f.write(output_html)
    
    print(f"Build Success! Published: {stats['total']}, Preprints: {len(preprints)}")

if __name__ == '__main__':
    build()
