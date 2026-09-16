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
    '[bad](mailto:attacker@example.com)',
  ])('removes unsafe link destinations: %s', (markdown) => {
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
    const html = parseMarkdown('[<img src=x onerror="alert(1)">](https://public.example/research)');

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

describe('Web Source citation presentation', () => {
  it('shows adjacent source links as bracketed numbers and preserves their destinations', () => {
    const container = document.createElement('div');
    container.innerHTML = parseMarkdown(
      '来源 [S1](<https://public.example/one>)[S2](<https://public.example/two>)，再次引用 [S1](<https://public.example/one>)，另见 [S12](<https://public.example/twelve>)。',
      7
    );

    expect(container.textContent?.trim()).toBe('来源 [1][2]，再次引用 [1]，另见 [12]。');
    const links = [...container.querySelectorAll('a')];
    expect(links.map((link) => link.getAttribute('href'))).toEqual([
      'https://public.example/one',
      'https://public.example/two',
      'https://public.example/one',
      'https://public.example/twelve',
    ]);
    for (const link of links) {
      expect(link.getAttribute('target')).toBe('_blank');
      expect(link.getAttribute('rel')).toBe('noopener noreferrer');
    }
    expect(container.querySelector('.cite-ref')).toBeNull();
  });

  it('keeps prose, code, ordinary links, and knowledge citations unchanged', () => {
    const container = document.createElement('div');
    container.innerHTML = parseMarkdown(
      'S1S2 [S1] `S2` [Section S1](https://public.example/section) [1, 2]\n\n```text\n[S1](https://public.example/code)\n```',
      7
    );

    expect(container.textContent).toContain('S1S2 [S1] S2 Section S1 [1][2]');
    expect(container.querySelector('pre code')?.textContent).toBe(
      '[S1](https://public.example/code)'
    );
    expect(container.querySelector('a')?.textContent).toBe('Section S1');
    expect([...container.querySelectorAll('.cite-ref')].map((ref) => ref.textContent)).toEqual([
      '[1]',
      '[2]',
    ]);
  });

  it('does not make an unsafe source destination clickable', () => {
    const container = document.createElement('div');
    container.innerHTML = parseMarkdown('[S1](javascript:alert(1))', 7);

    expect(container.querySelector('a')?.hasAttribute('href')).toBe(false);
    expect(container.querySelector('.cite-ref')).toBeNull();
  });
});
