// @vitest-environment jsdom

import { describe, expect, it } from 'vitest';

import { parseMarkdown } from './markdown';

describe('parseMarkdown security', () => {
  it('removes raw HTML, event handlers, and active content', () => {
    const html = parseMarkdown(
      '<img src=x onerror="alert(1)"><script>alert(2)</script><svg onload="alert(3)"></svg>'
    );

    expect(html).not.toContain('<img');
    expect(html).not.toContain('<script');
    expect(html).not.toContain('<svg');
    expect(html).not.toContain('onerror');
    expect(html).not.toContain('onload');
  });

  it.each([
    '[bad](javascript:alert(1))',
    '[bad](data:text/html,<script>alert(1)</script>)',
    '[bad](//attacker.example/phish)',
    '[bad](mailto:attacker@example.com)'
  ])('removes unsafe link destinations: %s', markdown => {
    const html = parseMarkdown(markdown);

    expect(html).not.toMatch(/href=/i);
    expect(html).not.toContain('javascript:');
    expect(html).not.toContain('data:text');
    expect(html).not.toContain('//attacker.example');
  });

  it('keeps only safe http links and hardens the new browsing context', () => {
    const html = parseMarkdown('[source](https://public.example/research)');

    expect(html).toContain('href="https://public.example/research"');
    expect(html).toContain('target="_blank"');
    expect(html).toContain('rel="noopener noreferrer"');
  });

  it('does not let an untrusted Markdown label create nested HTML', () => {
    const html = parseMarkdown(
      '[<img src=x onerror="alert(1)">](https://public.example/research)'
    );

    expect(html).not.toContain('<img');
    expect(html).not.toContain('onerror');
    expect(html).toContain('href="https://public.example/research"');
  });

  it('preserves generated citation references but not arbitrary attributes', () => {
    const html = parseMarkdown('Grounded claim [1].', 7);

    expect(html).toContain('class="cite-ref"');
    expect(html).toContain('data-msg-index="7"');
    expect(html).toContain('data-chunk-index="1"');
  });
});
