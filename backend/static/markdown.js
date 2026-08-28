/* ========= 极简 Markdown 渲染器 =========
   给公告栏用。先转义再解析，输出可以直接 innerHTML。
   支持：# ~ ###### 标题 / **粗体** / *斜体* / ~~删除线~~ / `行内代码`
        ``` 代码块 / - * + 无序列表 / 1. 有序列表 / > 引用 / --- 分隔线
        [文字](链接) / 换行
*/
(function(global){

const PH = '\u0000'; // 行内代码占位符，正文里不会出现

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

function inline(text){
  const codes = [];
  let s = esc(text);

  // 行内代码先抽出来，免得里面的星号被当成格式
  s = s.replace(/`([^`\n]+)`/g, (m, c) => PH + (codes.push(c) - 1) + PH);

  s = s.replace(/!?\[([^\]\n]*)\]\(([^)\s]+)\)/g, (m, label, url) => {
    const href = safeUrl(url);
    if(!href) return label;
    return '<a href="' + href + '" target="_blank" rel="noopener noreferrer">' + (label || href) + '</a>';
  });
  s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/__([^_\n]+)__/g, '<strong>$1</strong>');
  s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
  s = s.replace(/(^|[^\w])_([^_\n]+)_/g, '$1<em>$2</em>');
  s = s.replace(/~~([^~\n]+)~~/g, '<del>$1</del>');

  return s.replace(new RegExp(PH + '(\\d+)' + PH, 'g'), (m, i) => '<code>' + codes[+i] + '</code>');
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
