/* ========= 极简 Markdown 渲染器 =========
   给公告栏用。先转义再解析，输出可以直接 innerHTML。
   支持：# ~ ###### 标题 / **粗体** / *斜体* / ~~删除线~~ / `行内代码`
        ``` 代码块 / - * + 无序列表 / 1. 有序列表 / > 引用 / --- 分隔线
        [文字](链接) / 裸链接自动识别 / 换行
*/
(function(global){

const PH = '\u0000';  // 行内代码占位符，正文里不会出现
const PHA = '\u0001'; // 已生成的 <a> 占位符，免得被后面的规则再动一次

// 裸链接：域名允许中文（如 https://南林图书馆.中国/x.apk），
// 路径之后只认 ASCII，免得把紧跟其后的中文正文一并吞掉
const BARE_URL = new RegExp(
  'https?://[^\\s\\u0000\\u0001<>"\'*/?#，。；：！？、）】》「」…]+' +
  '(?:[/?#][A-Za-z0-9\\-._~:/?#\\[\\]@!$&\'()+,;=%]*)?', 'g');
// 句末标点不算链接的一部分。不含 ; 是怕截断 &amp; 这类实体，右括号另外按配对处理
const URL_TAIL = /[.,:!?'"]+$/;

function esc(s){
  return (s == null ? '' : String(s))
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// 只放行 http(s) / mailto / 站内路径，挡掉 javascript: 之类
function safeUrl(u){
  const t = String(u || '').trim();
  return /^(https?:\/\/|mailto:|\/|#)/i.test(t) ? t : '';
}

function cnt(s, ch){ return s.split(ch).length - 1; }

function anchor(href, label){
  return '<a href="' + href + '" target="_blank" rel="noopener noreferrer">' + label + '</a>';
}

function inline(text){
  const codes = [], links = [];
  const keep = html => PHA + (links.push(html) - 1) + PHA;
  let s = esc(text);

  // 行内代码先抽出来，免得里面的星号被当成格式
  s = s.replace(/`([^`\n]+)`/g, (m, c) => PH + (codes.push(c) - 1) + PH);

  s = s.replace(/!?\[([^\]\n]*)\]\(([^)\s]+)\)/g, (m, label, url) => {
    const href = safeUrl(url);
    if(!href) return label;
    return keep(anchor(href, label || href));
  });

  // 裸链接自动变超链接。放在强调规则之前，URL 里的 _ * 就不会被当成格式了
  s = s.replace(BARE_URL, m => {
    // 收尾的 &quot; 之类是转义后的引号，不属于链接
    let url = m.replace(/(?:&(?:quot|amp|lt|gt);)+$/, '').replace(URL_TAIL, '');
    // 「(见 https://a.cn/x)」这种，多出来的右括号还给正文
    while(/\)$/.test(url) && cnt(url, ')') > cnt(url, '(')) url = url.slice(0, -1);
    if(!safeUrl(url)) return m;
    return keep(anchor(url, url)) + m.slice(url.length);
  });
  s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/__([^_\n]+)__/g, '<strong>$1</strong>');
  s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
  s = s.replace(/(^|[^\w])_([^_\n]+)_/g, '$1<em>$2</em>');
  s = s.replace(/~~([^~\n]+)~~/g, '<del>$1</del>');

  s = s.replace(new RegExp(PH + '(\\d+)' + PH, 'g'), (m, i) => '<code>' + codes[+i] + '</code>');
  return s.replace(new RegExp(PHA + '(\\d+)' + PHA, 'g'), (m, i) => links[+i]);
}

function renderMarkdown(src){
  const lines = String(src == null ? '' : src).replace(/\r\n?/g, '\n').split('\n');
  const ul = /^\s*[-*+]\s+/, ol = /^\s*\d+[.)]\s+/;
  const out = [];
  let i = 0;

  while(i < lines.length){
    const line = lines[i];

    // 代码块
    if(/^\s*```/.test(line)){
      const buf = [];
      i++;
      while(i < lines.length && !/^\s*```/.test(lines[i])) buf.push(lines[i++]);
      i++; // 吃掉收尾的 ```
      out.push('<pre><code>' + esc(buf.join('\n')) + '</code></pre>');
      continue;
    }

    if(!line.trim()){ i++; continue; }

    // 分隔线
    if(/^\s*([-*_])\s*(\1\s*){2,}$/.test(line)){ out.push('<hr>'); i++; continue; }

    // 标题
    const h = line.match(/^\s*(#{1,6})\s+(.*)$/);
    if(h){
      const lv = Math.min(h[1].length + 2, 6); // # 对应 h3，别在卡片里太抢眼
      out.push('<h' + lv + '>' + inline(h[2].trim()) + '</h' + lv + '>');
      i++;
      continue;
    }

    // 引用
    if(/^\s*>\s?/.test(line)){
      const buf = [];
      while(i < lines.length && /^\s*>\s?/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ''));
      out.push('<blockquote>' + renderMarkdown(buf.join('\n')) + '</blockquote>');
      continue;
    }

    // 列表
    if(ul.test(line) || ol.test(line)){
      const ordered = !ul.test(line);
      const marker = ordered ? ol : ul;
      const items = [];
      while(i < lines.length && marker.test(lines[i])){
        const buf = [lines[i++].replace(marker, '')];
        // 缩进的续行并进同一条
        while(i < lines.length && /^\s{2,}\S/.test(lines[i]) && !ul.test(lines[i]) && !ol.test(lines[i])){
          buf.push(lines[i++].trim());
        }
        items.push('<li>' + inline(buf.join(' ')) + '</li>');
      }
      const tag = ordered ? 'ol' : 'ul';
      out.push('<' + tag + '>' + items.join('') + '</' + tag + '>');
      continue;
    }

    // 段落：连续非空行合成一段，行内换行保留
    const para = [];
    while(i < lines.length && lines[i].trim()
          && !/^\s*(#{1,6}\s|>|```)/.test(lines[i])
          && !ul.test(lines[i]) && !ol.test(lines[i])){
      para.push(lines[i++]);
    }
    out.push('<p>' + para.map(l => inline(l.trim())).join('<br>') + '</p>');
  }

  return out.join('');
}

global.renderMarkdown = renderMarkdown;

})(typeof window !== 'undefined' ? window : this);
