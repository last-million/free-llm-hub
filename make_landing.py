import os

path = r'C:\Users\hamza\OneDrive\Bureau\bureau 2024\ALL\python perso\free-llm-hub\templates\landing.html'
content = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Calvoun Free LLM Hub — Every model. One gateway. Zero cost.</title>
<meta name="description" content="A free, local, unified gateway to every major LLM — OpenAI, Claude, Gemini, DeepSeek and more — through one clean interface, one API, and one budget. No subscriptions.">
<meta name="theme-color" content="#07090f">
<link rel="icon" type="image/webp" sizes="32x32" href="/static/favicon-32.webp">
<link rel="icon" type="image/webp" sizes="180x180" href="/static/favicon-180.webp">
<style>
:root {
  color-scheme: dark;
  --bg: #07090f;
  --bg-2: #0a0e17;
  --surface: #0e1420;
  --surface-2: #131b2b;
  --line: rgba(148,180,255,.10);
  --line-strong: rgba(148,180,255,.18);
  --text: #e8edf6;
  --text-dim: #9aa8c0;
  --text-faint: #5f6e86;
  --accent: #22C55E;
  --accent-2: #4ADE80;
  --cyan: #38bdf8;
  --violet: #a78bfa;
  --glow: rgba(34,197,94,.16);
  --glow-cy: rgba(56,189,248,.12);
  --radius: 14px;
  --sans: -apple-system, BlinkMacSystemFont, 'Segoe UI', Inter, Roboto, sans-serif;
  --mono: ui-monospace, 'Cascadia Code', Menlo, Consolas, monospace;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
html { scroll-behavior: smooth; }
body { background: var(--bg); color: var(--text); font-family: var(--sans); line-height: 1.6; overflow-x: hidden; }

.parallax-bg {
  position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; z-index: -1;
  background: radial-gradient(circle at 50% 20%, #111e38 0%, #07090f 70%);
  overflow: hidden;
}
.parallax-bg::after {
  content: ''; position: absolute; inset: 0;
  background-image: radial-gradient(rgba(56,189,248,.15) 1px, transparent 1px);
  background-size: 40px 40px;
  opacity: .4;
}

nav {
  position: fixed; top: 0; left: 0; right: 0; z-index: 100;
  display: flex; align-items: center; justify-content: space-between;
  padding: 1.25rem 5%;
  background: rgba(7, 9, 15, 0.85);
  backdrop-filter: blur(16px);
  border-bottom: 1px solid var(--line);
}
.nav-brand { display: flex; align-items: center; gap: .75rem; text-decoration: none; color: var(--text); font-weight: 700; font-size: 1.15rem; }
.nav-brand img { width: 32px; height: 32px; border-radius: 8px; }
.nav-links { display: flex; align-items: center; gap: 2rem; list-style: none; }
.nav-links a { color: var(--text-dim); text-decoration: none; font-size: .95rem; transition: color .2s; }
.nav-links a:hover { color: var(--text); }
.btn {
  display: inline-flex; align-items: center; gap: .5rem;
  padding: .75rem 1.5rem; border-radius: var(--radius);
  font-weight: 600; font-size: .95rem; text-decoration: none;
  transition: all .2s cubic-bezier(.16,1,.3,1); cursor: pointer; border: none;
}
.btn-primary { background: var(--accent); color: #04110A; box-shadow: 0 0 24px rgba(34,197,94,.3); }
.btn-primary:hover { background: var(--accent-2); transform: translateY(-2px); box-shadow: 0 0 32px rgba(34,197,94,.5); }
.btn-outline { background: transparent; color: var(--text); border: 1px solid var(--line-strong); }
.btn-outline:hover { background: var(--surface); border-color: var(--cyan); color: var(--cyan); }

header.hero {
  position: relative; min-height: 100vh; display: flex; align-items: center; justify-content: center;
  text-align: center; padding: 8rem 5% 4rem; z-index: 1;
}
.hero-content { max-width: 900px; margin: 0 auto; }
.badge {
  display: inline-flex; align-items: center; gap: .5rem;
  padding: .35rem 1rem; border-radius: 100px;
  background: rgba(34, 197, 94, 0.1); border: 1px solid rgba(34, 197, 94, 0.3);
  color: var(--accent-2); font-size: .85rem; font-weight: 600; margin-bottom: 1.5rem;
  animation: pulseGlow 3s infinite;
}
@keyframes pulseGlow { 0%, 100% { box-shadow: 0 0 0 0 rgba(34,197,94,0.4); } 50% { box-shadow: 0 0 0 10px rgba(34,197,94,0); } }

h1 {
  font-size: clamp(2.75rem, 6vw, 5.25rem);
  font-weight: 800; line-height: 1.1; letter-spacing: -0.03em;
  margin-bottom: 1.5rem;
  background: linear-gradient(135deg, #fff 30%, #9aa8c0 100%);
  -webkit-background-clip: text; -webkit-text-fill-color: transparent;
}
h1 span {
  background: linear-gradient(135deg, var(--accent) 0%, var(--cyan) 100%);
  -webkit-background-clip: text; -webkit-text-fill-color: transparent;
}
p.hero-desc {
  font-size: clamp(1.125rem, 2vw, 1.35rem); color: var(--text-dim);
  max-width: 700px; margin: 0 auto 2.5rem; line-height: 1.7;
}
.hero-cta { display: flex; gap: 1rem; justify-content: center; flex-wrap: wrap; margin-bottom: 4rem; }

.hero-preview {
  position: relative; border-radius: 20px; overflow: hidden;
  border: 1px solid var(--line-strong); background: var(--surface);
  box-shadow: 0 30px 100px rgba(0,0,0,.6), 0 0 40px var(--glow);
  transform: perspective(1000px) rotateX(4deg);
  transition: transform .5s cubic-bezier(.16,1,.3,1);
}
.hero-preview:hover { transform: perspective(1000px) rotateX(0deg) translateY(-4px); }
.hero-preview img { width: 100%; height: auto; display: block; max-height: 520px; object-fit: cover; filter: brightness(.9) contrast(1.05); }

section { padding: 7rem 5%; position: relative; z-index: 1; }
.section-title { text-align: center; max-width: 700px; margin: 0 auto 4rem; }
.section-title h2 { font-size: clamp(2rem, 3.5vw, 3rem); font-weight: 700; margin-bottom: 1rem; letter-spacing: -0.02em; }
.section-title p { color: var(--text-dim); font-size: 1.1rem; }

.bento-grid {
  display: grid; grid-template-columns: repeat(12, 1fr); gap: 1.5rem; max-width: 1200px; margin: 0 auto;
}
.bento-card {
  background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius);
  padding: 2.5rem; display: flex; flex-direction: column; justify-content: space-between;
  position: relative; overflow: hidden; transition: border-color .3s, transform .3s;
}
.bento-card:hover { border-color: var(--line-strong); transform: translateY(-3px); }
.bento-card.col-4 { grid-column: span 4; }
.bento-card.col-6 { grid-column: span 6; }
.bento-card.col-8 { grid-column: span 8; }
.bento-card.col-12 { grid-column: span 12; }
@media(max-width: 900px) {
  .bento-card.col-4, .bento-card.col-6, .bento-card.col-8 { grid-column: span 12; }
}
.bento-icon {
  width: 48px; height: 48px; border-radius: 10px; background: var(--surface-2);
  display: flex; align-items: center; justify-content: center; font-size: 1.5rem;
  margin-bottom: 1.5rem; border: 1px solid var(--line); color: var(--accent);
}
.bento-card h3 { font-size: 1.35rem; font-weight: 600; margin-bottom: .75rem; }
.bento-card p { color: var(--text-dim); font-size: .95rem; line-height: 1.6; }
.bento-img { margin-top: 2rem; border-radius: 10px; overflow: hidden; border: 1px solid var(--line); }
.bento-img img { width: 100%; height: 220px; object-fit: cover; display: block; }

.stats-band {
  background: var(--bg-2); border-top: 1px solid var(--line); border-bottom: 1px solid var(--line);
  padding: 4rem 5%;
}
.stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 2rem; max-width: 1100px; margin: 0 auto; text-align: center; }
.stat-item h3 { font-size: 3rem; font-weight: 800; color: #fff; margin-bottom: .25rem; font-family: var(--mono); }
.stat-item h3 span { color: var(--accent); }
.stat-item p { color: var(--text-dim); font-size: .95rem; }

.code-section { max-width: 900px; margin: 0 auto; }
.code-box {
  background: #04060a; border: 1px solid var(--line-strong); border-radius: var(--radius);
  overflow: hidden; box-shadow: 0 20px 50px rgba(0,0,0,.5);
}
.code-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 1rem 1.25rem; background: var(--surface); border-bottom: 1px solid var(--line);
  font-family: var(--mono); font-size: .85rem; color: var(--text-dim);
}
.code-dots { display: flex; gap: .5rem; }
.code-dot { width: 10px; height: 10px; border-radius: 50%; background: var(--line-strong); }
.code-body { padding: 1.5rem; font-family: var(--mono); font-size: .9rem; overflow-x: auto; color: #a5b4fc; line-height: 1.7; }
.code-body .kw { color: #f472b6; }
.code-body .str { color: #86efac; }

.cases-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 2rem; max-width: 1200px; margin: 0 auto; }
.case-card {
  background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius);
  padding: 2rem; position: relative;
}
.case-card p { font-style: italic; color: var(--text); margin-bottom: 1.5rem; font-size: 1.05rem; line-height: 1.7; }
.case-author { display: flex; align-items: center; gap: 1rem; }
.case-author img { width: 44px; height: 44px; border-radius: 50%; object-fit: cover; }
.case-author h4 { font-size: .95rem; font-weight: 600; }
.case-author span { font-size: .85rem; color: var(--text-faint); }

footer {
  background: var(--bg-2); border-top: 1px solid var(--line);
  padding: 5rem 5% 3rem; text-align: center; position: relative; z-index: 1;
}
footer p { color: var(--text-faint); font-size: .9rem; margin-top: 2rem; }
</style>
</head>
<body>

<div class="parallax-bg"></div>

<nav>
  <a href="/" class="nav-brand">
    <img src="/static/calvoun-logo.webp" alt="Logo">
    Free LLM Hub
  </a>
  <ul class="nav-links">
    <li><a href="#features">Features</a></li>
    <li><a href="#quickstart">Quickstart</a></li>
  </ul>
  <div>
    <a href="/hub" class="btn btn-outline" style="margin-right:.75rem;">Dashboard</a>
    <a href="/chat" class="btn btn-primary">Launch Hub &rarr;</a>
  </div>
</nav>

<header class="hero">
  <div class="hero-content">
    <div class="badge">
      <span>⚡</span> Local Gateway & API Multiplexer
    </div>
    <h1>Every Major LLM.<br><span>One Unified Gateway. Zero Cost.</span></h1>
    <p class="hero-desc">
      Access OpenAI, Claude, Gemini, DeepSeek, and local models seamlessly through a single local interface and compatible OpenAI API. No subscriptions. No keys juggling. Just models.
    </p>
    <div class="hero-cta">
      <a href="/chat" class="btn btn-primary" style="padding: 1.05rem 2.25rem; font-size: 1.05rem;">Launch Hub Now &rarr;</a>
      <a href="#quickstart" class="btn btn-outline" style="padding: 1.05rem 2.25rem; font-size: 1.05rem;">View API Docs</a>
    </div>

    <div class="hero-preview">
      <img src="/static/neural-brand.webp" alt="Neural Network AI">
    </div>
  </div>
</header>

<section class="stats-band">
  <div class="stats-grid">
    <div class="stat-item">
      <h3>100<span>%</span></h3>
      <p>Open & Local First</p>
    </div>
    <div class="stat-item">
      <h3>0<span>$</span></h3>
      <p>No Subscription Lock-in</p>
    </div>
    <div class="stat-item">
      <h3>10<span>+</span></h3>
      <p>Supported AI Providers</p>
    </div>
    <div class="stat-item">
      <h3>&lt;2<span>ms</span></h3>
      <p>Gateway Routing Latency</p>
    </div>
  </div>
</section>

<section id="features">
  <div class="section-title">
    <h2>Engineered for Power & Control</h2>
    <p>Everything you need to orchestrate models locally without friction.</p>
  </div>

  <div class="bento-grid">
    <div class="bento-card col-8">
      <div>
        <div class="bento-icon">🧠</div>
        <h3>Universal API Compatibility</h3>
        <p>Drop-in replacement for OpenAI endpoints. Point any SDK, agent, or client app (`/v1/chat/completions`, `/v1/messages`) directly to your local hub and let it handle provider routing, fallback, and key management.</p>
      </div>
      <div class="bento-img">
        <img src="/static/stock-coding.jpg" alt="Coding">
      </div>
    </div>

    <div class="bento-card col-4">
      <div>
        <div class="bento-icon">⚡</div>
        <h3>Smart Failover & Routing</h3>
        <p>Automatically retries across available free and paid keys so your requests never drop when rate limits hit.</p>
      </div>
    </div>

    <div class="bento-card col-4">
      <div>
        <div class="bento-icon">🛡️</div>
        <h3>Quota & Budget Guard</h3>
        <p>Granular token tracking and usage limits prevent unexpected provider bills across all sessions.</p>
      </div>
    </div>

    <div class="bento-card col-8">
      <div>
        <div class="bento-icon">🌐</div>
        <h3>Multi-Agent Swarm Orchestration</h3>
        <p>Spawn parallel agent crews to tackle complex research, code reviews, and automated workflows right from your dashboard.</p>
      </div>
      <div class="bento-img">
        <img src="/static/stock-server.jpg" alt="Data Center">
      </div>
    </div>
  </div>
</section>

<section id="quickstart">
  <div class="section-title">
    <h2>Instant API Integration</h2>
    <p>Connect your favorite tools in seconds.</p>
  </div>

  <div class="code-section">
    <div class="code-box">
      <div class="code-header">
        <span>Python / OpenAI SDK Client</span>
        <div class="code-dots"><div class="code-dot"></div><div class="code-dot"></div><div class="code-dot"></div></div>
      </div>
      <div class="code-body">
        <span class="kw">from</span> openai <span class="kw">import</span> OpenAI<br><br>
        client = OpenAI(<br>
        &nbsp;&nbsp;base_url=<span class="str">"http://127.0.0.1:8787/v1"</span>,<br>
        &nbsp;&nbsp;api_key=<span class="str">"any-local-token"</span><br>
        )<br><br>
        response = client.chat.completions.create(<br>
        &nbsp;&nbsp;model=<span class="str">"auto"</span>,<br>
        &nbsp;&nbsp;messages=[{"role": "user", "content": "Hello from Free LLM Hub!"}]<br>
        )<br>
        print(response.choices[0].message.content)
      </div>
    </div>
  </div>
</section>

<footer>
  <h3>Calvoun Free LLM Hub</h3>
  <p>&copy; 2026 Free LLM Hub. Open local gateway. Powered by Python & Flask.</p>
</footer>

</body>
</html>
'''
with open(path, 'w', encoding='utf-8') as f:
    f.write(content)
print('Successfully wrote landing.html')
