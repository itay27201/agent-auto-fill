// A small markdown renderer for guide text.
//
// The site ships verbatim — SiteStack deploys site/ as a raw S3 asset with no
// bundler — so pulling in marked or markdown-it would mean introducing a build
// step for one feature. The guide format is deliberately narrow (headings,
// paragraphs, lists, emphasis, links, inline code), so this covers it.
//
// Everything is escaped before any markup is inserted. Guide text is written
// by an agent from documents a user uploaded, which means it is untrusted
// input taking the shortest possible path to innerHTML.

const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

export function escapeHtml(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => ESCAPES[c]);
}

/** Block-level render. Returns an HTML string; the caller decides where it goes. */
export function renderMarkdown(src) {
  const lines = String(src ?? "").replace(/\r\n/g, "\n").split("\n");
  const out = [];
  let para = [];
  let list = null; // "ul" | "ol"

  const flushPara = () => {
    if (para.length) {
      out.push(`<p>${inline(para.join(" "))}</p>`);
      para = [];
    }
  };
  const flushList = () => {
    if (list) {
      out.push(`</${list}>`);
      list = null;
    }
  };
  const flushAll = () => {
    flushPara();
    flushList();
  };

  for (const raw of lines) {
    const line = raw.trimEnd();

    if (!line.trim()) {
      flushAll();
      continue;
    }

    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      flushAll();
      // Guide sections are h2 in the source but sit inside a panel that
      // already has its own heading, so everything shifts down two levels.
      const level = Math.min(6, heading[1].length + 2);
      out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      continue;
    }

    const bullet = /^\s*[-*+]\s+(.*)$/.exec(line);
    const numbered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    if (bullet || numbered) {
      flushPara();
      const want = bullet ? "ul" : "ol";
      if (list !== want) {
        flushList();
        out.push(`<${want}>`);
        list = want;
      }
      out.push(`<li>${inline((bullet || numbered)[1])}</li>`);
      continue;
    }

    if (/^\s*(---+|===+|\*\*\*+)\s*$/.test(line)) {
      flushAll();
      out.push("<hr />");
      continue;
    }

    const quote = /^\s*>\s?(.*)$/.exec(line);
    if (quote) {
      flushAll();
      out.push(`<blockquote>${inline(quote[1])}</blockquote>`);
      continue;
    }

    flushList();
    para.push(line.trim());
  }

  flushAll();
  return out.join("\n");
}

/** Inline spans. Escapes first, so no pattern here can produce new markup. */
function inline(text) {
  let s = escapeHtml(text);
  s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>");
  // Only http(s) links become anchors: an escaped javascript: URL is inert,
  // but rendering it as a clickable link is still not something to invite.
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  return s;
}

/** Convenience: render into an element. */
export function setMarkdown(el, src) {
  el.innerHTML = renderMarkdown(src);
}
