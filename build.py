#!/usr/bin/env python3
"""Static site builder for paulharris.ca — builds from wp-archive/json into the repo root.

Content rule: every post/page body is Chip's verbatim content.rendered, with
WordPress plumbing stripped (junk hidden inputs, editor comments, srcset noise)
and image/internal URLs rewritten to local paths. No invented copy.
"""
import json, re, os, sys, shutil, subprocess, html
from datetime import datetime
from collections import defaultdict

ROOT = os.path.dirname(os.path.abspath(__file__))
J = lambda *p: os.path.join(ROOT, *p)
MEDIA = J('wp-archive', 'media')
IMG = J('img')

posts = json.load(open(J('wp-archive/json/posts.json')))
pages = json.load(open(J('wp-archive/json/pages.json')))
comments = json.load(open(J('wp-archive/json/comments.json')))
categories = json.load(open(J('wp-archive/json/categories.json')))

posts = [p for p in posts if p['status'] == 'publish']
posts.sort(key=lambda p: p['date'], reverse=True)
catname = {c['id']: c['name'] for c in categories}

# ---------------------------------------------------------------- media index
os.makedirs(IMG, exist_ok=True)
media_files = os.listdir(MEDIA)
# key: filename with `{id}-` / `orphan-` prefix removed  ->  actual filename
local_by_stem = {}
for f in sorted(media_files):
    stem = re.sub(r'^(?:\d+|orphan)-', '', f)
    local_by_stem.setdefault(stem, f)

_processed = {}   # media filename -> output filename in img/
_stats = {'copied': 0, 'resized': 0, 'jpegged': 0}

def sips_width(path):
    out = subprocess.run(['sips', '-g', 'pixelWidth', path], capture_output=True, text=True).stdout
    m = re.search(r'pixelWidth: (\d+)', out)
    return int(m.group(1)) if m else 0

def process_media(fname):
    """Copy wp-archive/media/<fname> into img/, downscaling / recompressing. Returns output name."""
    if fname in _processed:
        return _processed[fname]
    src = os.path.join(MEDIA, fname)
    out = fname
    ext = fname.rsplit('.', 1)[-1].lower()
    dst = os.path.join(IMG, out)
    if ext in ('jpg', 'jpeg', 'png'):
        w = sips_width(src)
        heavy_png = ext == 'png' and os.path.getsize(src) > 300_000
        if heavy_png:
            out = re.sub(r'\.png$', '.jpg', fname, flags=re.I)
            dst = os.path.join(IMG, out)
        if not os.path.exists(dst):
            ops = []
            if w > 1600:
                ops += ['--resampleWidth', '1600']
                _stats['resized'] += 1
            if heavy_png:
                ops += ['-s', 'format', 'jpeg', '-s', 'formatOptions', '82']
                _stats['jpegged'] += 1
            if ops:
                subprocess.run(['sips'] + ops + [src, '--out', dst], capture_output=True)
                if not os.path.exists(dst):
                    print('SIPS FAILED:', fname, file=sys.stderr)
                    shutil.copy2(src, dst)
            else:
                shutil.copy2(src, dst)
        _stats['copied'] += 1
    else:
        if not os.path.exists(dst):
            shutil.copy2(src, dst)
        _stats['copied'] += 1
    _processed[fname] = out
    return out

unmatched_uploads = set()

def local_upload(url):
    """Map a wp-content/uploads URL to a local /img/ path, or None."""
    base = os.path.basename(url.split('?')[0])
    stripped = re.sub(r'-\d+x\d+(?=\.\w+$)', '', base)
    f = local_by_stem.get(stripped) or local_by_stem.get(base)
    if not f:
        unmatched_uploads.add(url)
        return None
    return '/img/' + process_media(f)

# ---------------------------------------------------------------- URL routing
post_by_slug = {p['slug']: p for p in posts}
page_path = {}           # page id -> full path like 'recipes/drinks/mai-tai'
for p in pages:
    path = re.sub(r'https?://(?:www\.)?paulharris\.ca/', '', p['link']).strip('/')
    page_path[p['id']] = path
page_paths = set(page_path.values())
post_id_slug = {p['id']: p['slug'] for p in posts}

def route_internal(path):
    """Map an internal WP path to the new site path."""
    path = path.split('#')[0]
    m = re.match(r'\?p=(\d+)$', path)
    if m:
        slug = post_id_slug.get(int(m.group(1)))
        return f'/writing/{slug}/' if slug else '/writing/'
    clean = path.strip('/')
    if clean == '':
        return '/'
    if clean in page_paths:
        return f'/{clean}/'
    last = clean.split('/')[-1]
    if last in post_by_slug:
        return f'/writing/{last}/'
    if last in page_paths:
        return f'/{last}/'
    return None

# ---------------------------------------------------------------- content cleaning
def clean_content(raw, ctx=''):
    h = raw
    # editor junk
    h = re.sub(r'<input[^>]*>', '', h)
    h = re.sub(r'<!--.*?-->', '', h, flags=re.S)
    h = re.sub(r'\s(?:loading|decoding|srcset|sizes)="[^"]*"', '', h)
    # rewrite upload URLs (src and href), collecting failures
    def sub_upload(m):
        url = m.group(0)
        loc = local_upload(url)
        return loc if loc else url
    h = re.sub(r'https?://[^"\s]*?/wp-content/uploads/[^"\s\)>]+', sub_upload, h)
    # drop images (and their wrapping anchors) whose file could not be recovered
    h = re.sub(r'<a[^>]*href="https?://[^"]*wp-content[^"]*"[^>]*>\s*<img[^>]*>\s*</a>', '', h)
    h = re.sub(r'<img[^>]*src="https?://[^"]*wp-content[^"]*"[^>]*>', '', h)
    h = re.sub(r'(<a[^>]*)href="https?://[^"]*wp-content[^"]*"([^>]*>)', r'\1\2', h)
    # unwrap anchors that just link an image to its own full-size file
    h = re.sub(r'<a[^>]*href="/img/[^"]*"[^>]*>\s*(<img[^>]*>)\s*</a>', r'\1', h)
    # internal links -> new routes
    def sub_link(m):
        tail = m.group(2)
        r = route_internal(tail)
        return 'href="%s"' % (r if r else '/writing/')
    h = re.sub(r'href="https?://(www\.)?paulharris\.ca/([^"]*)"', sub_link, h)
    # empty paragraphs left by stripped junk
    h = re.sub(r'<p[^>]*>(?:\s|&nbsp;|<br ?/?>)*</p>', '', h)
    return h.strip()

def text_of(h):
    t = re.sub(r'<[^>]+>', ' ', h)
    t = html.unescape(t)
    return re.sub(r'\s+', ' ', t).strip()

def excerpt(p, words=42):
    t = text_of(clean_content(p['content']['rendered']))
    ws = t.split()
    if len(ws) <= words:
        return t
    return ' '.join(ws[:words]).rstrip('.,;:—-') + ' …'

def fmt_date(iso):
    return datetime.fromisoformat(iso).strftime('%B %-d, %Y')

def esc(s):
    return s  # titles arrive HTML-rendered (entities intact); pass through

# ---------------------------------------------------------------- layout
NAV = [
    ('/', 'Home'),
    ('/writing/', 'Writing'),
    ('/becoming-italian/', 'Becoming Italian'),
    ('/recipes/', 'Food &amp; Drink'),
    ('/about-2/', 'Biography'),
    ('/contact-me/', 'Contact'),
]

FOOTER_MORE = [
    ('/sunworks/', 'Sunworks'),
    ('/my-prints-for-sale/', 'My prints for sale'),
    ('/the-aesthetics-of-identity-and-living/', 'The aesthetics of identity and living'),
    ('/contact-me/', 'Contact me'),
]

def page_shell(title, body, active='', desc='', depth_home=False):
    ACT = ' class="active"'
    navlinks = '\n        '.join(
        f'<a href="{h}"{ACT if h == active else ""}>{t}</a>' for h, t in NAV)
    more = ' <span class="dot">·</span> '.join(f'<a href="{h}">{t}</a>' for h, t in FOOTER_MORE)
    d = desc or 'observations and insights from living on earth in the company of solitary travellers'
    header = '' if depth_home else f'''<header class="site-head">
    <div class="wrap head-row">
      <a class="wordmark" href="/">Paul Harris<span class="mark">.</span></a>
      <nav class="site-nav">
        {navlinks}
      </nav>
    </div>
  </header>'''
    home_nav = f'''<nav class="site-nav home-nav">
        {navlinks}
      </nav>''' if depth_home else ''
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{d}">
<link rel="stylesheet" href="/style.css">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
</head>
<body>
  {header if not depth_home else home_nav}
  {body}
  <footer class="site-foot">
    <div class="wrap">
      <p class="foot-more">{more}</p>
      <p class="foot-fine">Paul (Chip) Harris · Red Deer, Alberta &amp; Chiavari, Italia · writing here since 2007</p>
    </div>
  </footer>
</body>
</html>
'''

def write(path, content):
    full = J(path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    open(full, 'w').write(content)

# ---------------------------------------------------------------- comments
comments_by_post = defaultdict(list)
for c in comments:
    if c['status'] == 'approved':
        comments_by_post[c['post']].append(c)
for v in comments_by_post.values():
    v.sort(key=lambda c: c['date'])

def render_comments(pid):
    cs = comments_by_post.get(pid)
    if not cs:
        return ''
    items = []
    for c in cs:
        body = clean_content(c['content']['rendered'])
        items.append(f'''<div class="comment{' comment-reply' if c['parent'] else ''}">
        <p class="comment-meta"><span class="comment-author">{c["author_name"]}</span> <span class="comment-date">{fmt_date(c["date"])}</span></p>
        <div class="comment-body">{body}</div>
      </div>''')
    n = len(cs)
    label = f'{n} comment' + ('' if n == 1 else 's')
    return f'''<section class="comments">
      <h2 class="comments-title">{label}</h2>
      {''.join(items)}
    </section>'''

# ---------------------------------------------------------------- post pages
rendered_comments = 0
for i, p in enumerate(posts):
    body_html = clean_content(p['content']['rendered'], p['slug'])
    cats = [catname[c] for c in p['categories'] if catname.get(c) and catname[c] != 'Uncategorized']
    cat_html = f' <span class="dot">·</span> <span class="cats">{", ".join(cats)}</span>' if cats else ''
    newer = posts[i - 1] if i > 0 else None
    older = posts[i + 1] if i < len(posts) - 1 else None
    pager = '<nav class="pager">'
    pager += (f'<a class="pager-prev" href="/writing/{older["slug"]}/"><span class="pager-label">Older</span>{esc(older["title"]["rendered"])}</a>' if older else '<span></span>')
    pager += (f'<a class="pager-next" href="/writing/{newer["slug"]}/"><span class="pager-label">Newer</span>{esc(newer["title"]["rendered"])}</a>' if newer else '<span></span>')
    pager += '</nav>'
    cm = render_comments(p['id'])
    if cm:
        rendered_comments += len(comments_by_post[p['id']])
    body = f'''<main class="wrap post">
    <article>
      <header class="post-head">
        <p class="post-meta">{fmt_date(p['date'])}{cat_html}</p>
        <h1 class="post-title">{esc(p['title']['rendered'])}</h1>
      </header>
      <div class="prose">
        {body_html}
      </div>
    </article>
    {cm}
    {pager}
  </main>'''
    write(f'writing/{p["slug"]}/index.html',
          page_shell(f'{text_of(p["title"]["rendered"])} — Paul Harris', body, active='/writing/',
                     desc=text_of(excerpt(p, 28))))

# ---------------------------------------------------------------- writing archive
by_year = defaultdict(list)
for p in posts:
    by_year[p['date'][:4]].append(p)
years = sorted(by_year, reverse=True)
yearnav = '\n        '.join(f'<a href="#y{y}">{y}</a>' for y in years)
sections = []
for y in years:
    items = []
    for p in by_year[y]:
        d = datetime.fromisoformat(p['date'])
        items.append(f'''<li><a class="arc-link" href="/writing/{p['slug']}/"><span class="arc-title">{esc(p['title']['rendered'])}</span><span class="arc-date">{d.strftime('%b %-d')}</span></a></li>''')
    n = len(by_year[y])
    sections.append(f'''<section class="arc-year" id="y{y}">
      <h2 class="year-head">{y}<span class="year-count">{n}</span></h2>
      <ul class="arc-list">
        {''.join(items)}
      </ul>
    </section>''')
body = f'''<main class="wrap archive">
    <header class="page-head">
      <h1>Writing</h1>
      <p class="page-sub"><em>Conscious Evolution</em> — observations and insights from living on earth in the company of solitary travellers</p>
      <nav class="year-nav">
        {yearnav}
      </nav>
    </header>
    {''.join(sections)}
  </main>'''
write('writing/index.html', page_shell('Writing — Paul Harris', body, active='/writing/'))

# ---------------------------------------------------------------- pages
page_children = defaultdict(list)
for p in pages:
    if p.get('parent'):
        page_children[p['parent']].append(p)

for p in pages:
    body_html = clean_content(p['content']['rendered'], p['slug'])
    # the becoming-italian page carried a dynamic "latest posts" block; rebuild it clean
    if p['slug'] == 'becoming-italian':
        def rebuild_latest(m):
            links = re.findall(r'wp-block-latest-posts__post-title" href="([^"]+)"[^>]*>([^<]+)<', m.group(0))
            lis = ''.join(f'<li><a href="{h}">{t}</a></li>' for h, t in links)
            return f'<ul class="page-postlist">{lis}</ul>'
        body_html = re.sub(r'<ul class="wp-block-latest-posts__list[^"]*">.*?</ul>', rebuild_latest, body_html, flags=re.S)
    kids = page_children.get(p['id'])
    kid_html = ''
    if kids:
        kids_sorted = sorted(kids, key=lambda k: text_of(k['title']['rendered']).lower())
        lis = ''.join(f'<li><a href="/{page_path[k["id"]]}/">{esc(k["title"]["rendered"])}</a></li>' for k in kids_sorted)
        kid_html = f'<ul class="page-postlist">{lis}</ul>'
    cm = render_comments(p['id'])
    if cm:
        rendered_comments += len(comments_by_post[p['id']])
    path = page_path[p['id']]
    active = '/' + path.split('/')[0] + '/'
    body = f'''<main class="wrap post">
    <article>
      <header class="post-head">
        <h1 class="post-title">{esc(p['title']['rendered'])}</h1>
      </header>
      <div class="prose">
        {body_html}
        {kid_html}
      </div>
    </article>
    {cm}
  </main>'''
    write(f'{path}/index.html',
          page_shell(f'{text_of(p["title"]["rendered"])} — Paul Harris', body, active=active))

# ---------------------------------------------------------------- home
about = next(p for p in pages if p['slug'] == 'about-2')
about_clean = clean_content(about['content']['rendered'])
# first real text paragraph of his biography, verbatim
intro_p = ''
for m in re.finditer(r'<p>(.*?)</p>', about_clean, re.S):
    inner = m.group(1)
    if '<img' in inner or '<figure' in inner:
        continue
    if len(text_of(inner)) > 100:
        intro_p = inner.strip()
        break

recent = posts[:8]
cards = []
for p in recent:
    cards.append(f'''<li class="home-card">
        <a href="/writing/{p['slug']}/">
          <p class="card-date">{fmt_date(p['date'])}</p>
          <h3 class="card-title">{esc(p['title']['rendered'])}</h3>
          <p class="card-excerpt">{excerpt(p)}</p>
        </a>
      </li>''')

body = f'''<main class="home">
    <section class="hero">
      <div class="wrap">
        <h1 class="hero-name">Paul <em>Harris</em></h1>
        <p class="hero-tag">observations and insights from living on earth in the company of solitary travellers</p>
      </div>
    </section>
    <section class="wrap home-intro">
      <div class="prose intro-prose">
        <p>{intro_p}</p>
      </div>
      <p class="intro-more"><a href="/about-2/">Full biography&thinsp;→</a></p>
    </section>
    <section class="wrap home-recent">
      <h2 class="section-label">Recent writing</h2>
      <ul class="home-cards">
        {''.join(cards)}
      </ul>
      <p class="all-link"><a href="/writing/">All writing — {len(posts)} pieces, 2007&#8202;–&#8202;2025&thinsp;→</a></p>
    </section>
  </main>'''
write('index.html', page_shell('Paul Harris — Conscious Evolution', body, active='/', depth_home=True))

# ---------------------------------------------------------------- favicon
write('favicon.svg', '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect width="64" height="64" rx="14" fill="#0E7C6B"/>
<text x="32" y="46" font-family="Georgia,serif" font-size="40" font-style="italic" text-anchor="middle" fill="#FBF8F2">P</text>
</svg>''')

print('posts built:', len(posts))
print('pages built:', len(pages))
print('comments rendered:', rendered_comments)
print('media:', _stats)
print('unmatched uploads:')
for u in sorted(unmatched_uploads):
    print('  ', u)
