import { marked } from 'marked';
import hljs from 'highlight.js/lib/core';
import bash from 'highlight.js/lib/languages/bash';
import cpp from 'highlight.js/lib/languages/cpp';
import csharp from 'highlight.js/lib/languages/csharp';
import css from 'highlight.js/lib/languages/css';
import go from 'highlight.js/lib/languages/go';
import java from 'highlight.js/lib/languages/java';
import javascript from 'highlight.js/lib/languages/javascript';
import json from 'highlight.js/lib/languages/json';
import markdown from 'highlight.js/lib/languages/markdown';
import plaintext from 'highlight.js/lib/languages/plaintext';
import python from 'highlight.js/lib/languages/python';
import rust from 'highlight.js/lib/languages/rust';
import sql from 'highlight.js/lib/languages/sql';
import typescript from 'highlight.js/lib/languages/typescript';
import xml from 'highlight.js/lib/languages/xml';
import yaml from 'highlight.js/lib/languages/yaml';
import DOMPurify from 'dompurify';

hljs.registerLanguage('bash', bash);
hljs.registerLanguage('cpp', cpp);
hljs.registerLanguage('csharp', csharp);
hljs.registerLanguage('css', css);
hljs.registerLanguage('go', go);
hljs.registerLanguage('java', java);
hljs.registerLanguage('javascript', javascript);
hljs.registerLanguage('json', json);
hljs.registerLanguage('markdown', markdown);
hljs.registerLanguage('plaintext', plaintext);
hljs.registerLanguage('python', python);
hljs.registerLanguage('rust', rust);
hljs.registerLanguage('sql', sql);
hljs.registerLanguage('typescript', typescript);
hljs.registerLanguage('xml', xml);
hljs.registerLanguage('yaml', yaml);

const MARKDOWN_ALLOWED_TAGS = [
  'a',
  'blockquote',
  'br',
  'code',
  'del',
  'em',
  'h1',
  'h2',
  'h3',
  'h4',
  'h5',
  'h6',
  'hr',
  'li',
  'ol',
  'p',
  'pre',
  'span',
  'strong',
  'sup',
  'table',
  'tbody',
  'td',
  'th',
  'thead',
  'tr',
  'ul',
];

const MARKDOWN_ALLOWED_ATTR = [
  'class',
  'data-chunk-index',
  'data-msg-index',
  'href',
  'rel',
  'target',
  'title',
];

function sanitizeMarkdownHtml(html: string): string {
  const sanitized = DOMPurify.sanitize(html, {
    ALLOWED_ATTR: MARKDOWN_ALLOWED_ATTR,
    ALLOWED_TAGS: MARKDOWN_ALLOWED_TAGS,
    ALLOWED_URI_REGEXP: /^https?:\/\//i,
    ALLOW_UNKNOWN_PROTOCOLS: false,
    FORBID_ATTR: ['src', 'srcset', 'style'],
    FORBID_TAGS: ['embed', 'form', 'iframe', 'img', 'math', 'object', 'style', 'svg'],
    SANITIZE_NAMED_PROPS: true,
  });
  const template = document.createElement('template');
  template.innerHTML = sanitized;
  template.content.querySelectorAll('a').forEach((anchor) => {
    const href = anchor.getAttribute('href') || '';
    if (!/^https?:\/\//i.test(href)) {
      anchor.removeAttribute('href');
      anchor.removeAttribute('target');
      anchor.removeAttribute('rel');
      return;
    }
    anchor.setAttribute('target', '_blank');
    anchor.setAttribute('rel', 'noopener noreferrer');
  });
  return template.innerHTML;
}

// Customize the code renderer in marked for syntax highlighting
const renderer = new marked.Renderer();
renderer.code = (code, language) => {
  const validLanguage = language && hljs.getLanguage(language) ? language : 'plaintext';
  const highlighted = hljs.highlight(code, { language: validLanguage }).value;
  return `<pre><code class="hljs language-${validLanguage}">${highlighted}</code></pre>`;
};
renderer.html = () => '';

marked.use({
  renderer,
  breaks: true,
  gfm: true,
});

export function parseMarkdown(text: string, msgIndex?: number | null): string {
  const html = marked.parse(text || '', { async: false }) as string;

  if (msgIndex === undefined || msgIndex === null) {
    return sanitizeMarkdownHtml(html);
  }

  let inCode = false;
  const withCitations = html
    .split(/(<[^>]*>)/)
    .map((part) => {
      if (part.startsWith('<')) {
        if (part.startsWith('<code') || part.startsWith('<pre')) inCode = true;
        if (part.startsWith('</code') || part.startsWith('</pre')) inCode = false;
        return part;
      }
      if (!inCode) {
        return part.replace(/\[([\d\s,]+)\]/g, (match: string, p1: string) => {
          const numbers = p1
            .split(',')
            .map((n: string) => n.trim())
            .filter((n: string) => /^\d+$/.test(n));
          if (numbers.length === 0) return match;
          return numbers
            .map(
              (n: string) =>
                `<sup class="cite-ref" data-msg-index="${Number.isSafeInteger(msgIndex) ? msgIndex : 0}" data-chunk-index="${n}">[${n}]</sup>`
            )
            .join('');
        });
      }
      return part;
    })
    .join('');
  return sanitizeMarkdownHtml(withCitations);
}

export function escapeHtml(text: string): string {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}
