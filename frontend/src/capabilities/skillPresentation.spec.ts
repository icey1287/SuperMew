import { describe, expect, it } from 'vitest';
import {
  skillCompactName,
  skillDisplayIcon,
  skillDisplayName,
  skillDisplaySummary,
} from './skillPresentation';

describe('skill presentation', () => {
  it('uses concise Chinese copy for registered skills', () => {
    expect(skillDisplayName('web-research')).toBe('网页调研');
    expect(skillCompactName('sandbox')).toBe('代码沙盒');
    expect(
      skillDisplaySummary({ name: 'sql-assistant', description: 'Analyze authorized data.' })
    ).toBe('对授权数据进行安全的只读分析');
    expect(skillDisplayIcon('weather')).toBe('fa-solid fa-cloud-sun');
  });

  it('extracts the Chinese portion of custom skill metadata', () => {
    const skill = {
      name: 'city-guide',
      description: 'Find useful city information，查询城市交通、景点与开放时间；不确定时说明。',
    };

    expect(skillDisplayName(skill.name, skill.description)).toBe('查询城市交通');
    expect(skillDisplaySummary(skill)).toBe('查询城市交通、景点与开放时间');
  });

  it('does not expose an English-only custom description to users', () => {
    const skill = { name: 'internal-helper', description: 'Run an internal workflow.' };

    expect(skillDisplayName(skill.name, skill.description)).toBe('自定义能力');
    expect(skillDisplaySummary(skill)).toBe('专用能力');
  });
});
