import yaml
import bibtexparser
from jinja2 import Environment, FileSystemLoader
import os
from collections import defaultdict

def load_data():
    # 1. 读取配置文件
    with open('data/config.yaml', 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    
    # 2. 读取评级字典
    with open('data/rankings.yaml', 'r', encoding='utf-8') as f:
        rankings = yaml.safe_load(f)

    # 3. 读取BibTeX
    with open('data/papers.bib', 'r', encoding='utf-8') as f:
        bib_db = bibtexparser.load(f)
    
    return config, rankings, bib_db.entries

def process_papers(papers, rankings):
    # # 按年份倒序
    # papers.sort(key=lambda x: x.get('year', '0000'), reverse=True)
    
    stats = defaultdict(int)
    stats['total'] = len(papers)
    
    processed_papers = []
    
    for p in papers:
        # 获取期刊/会议名称
        venue_name = p.get('journal') or p.get('booktitle') or "Unknown Venue"
        p['venue'] = venue_name
        
        # 清理标题中的大括号
        p['title'] = p.get('title', '').replace('{', '').replace('}', '')
        p['author'] = p.get('author', '').replace('and', ',') # 简单处理作者
        
        # 匹配评级
        p['tags'] = []
        p['color'] = 'gray'
        
        found_match = False
        for k, v in rankings.items():
            if k.lower() in venue_name.lower():
                p['tags'] = v['tags']
                p['color'] = v.get('color', 'gray')
                
                # 统计
                for tag in v['tags']:
                    if "CCF" in tag: stats['ccf_total'] += 1
                    if "CCF A" in tag: stats['ccf_a'] += 1
                    if "CCF B" in tag: stats['ccf_b'] += 1
                    if "JCR Q1" in tag: stats['jcr_q1'] += 1
                    if "JCR Q2" in tag: stats['jcr_q2'] += 1
                found_match = True
                break
        
        processed_papers.append(p)
        
    return processed_papers, stats

def build():
    config, rankings, papers_raw = load_data()
    papers, stats = process_papers(papers_raw, rankings)
    
    # 将统计数据注入config，方便模板调用
    config['stats'] = stats
    
    # 渲染模板
    env = Environment(loader=FileSystemLoader('.'))
    template = env.get_template('template.html')
    
    output_html = template.render(
        info=config['info'],
        bio=config['bio'],
        edu=config['education'],
        papers=papers,
        stats=stats
    )
    
    os.makedirs('output', exist_ok=True)
    with open('output/index.html', 'w', encoding='utf-8') as f:
        f.write(output_html)
    
    print(f"Build Success! Stats: Total={stats['total']}, CCF={stats['ccf_total']}")

if __name__ == '__main__':
    build()
