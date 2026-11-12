/**
 * `src/contract-plugin.ts` 的行为测试：`apply` 只注册一段全局提示词段，
 * 段名/顺序固定，正文取自单一来源（`contract-text.ts`）。
 *
 * 用假 ctx 记录 `systemPrompt.section` 调用——不在测试里造一个真的
 * `@deepseek-ai/dsh-system-prompt`（它是宿主服务，不是本包依赖）。
 */

import { describe, expect, it } from 'vitest';

import * as contractPlugin from '../src/contract-plugin.js';
import {
  READER_CONTRACT_SECTION_NAME,
  READER_CONTRACT_SECTION_ORDER,
} from '../src/contract-plugin.js';
import { READER_CONTRACT_V4 } from '../src/contract-text.js';

interface RecordedSection {
  name: string;
  order: number;
  text: string;
}

interface FakeCtx {
  systemPrompt: {
    section(section: RecordedSection): () => void;
  };
}

function recordingCtx(): {
  ctx: FakeCtx;
  sections: RecordedSection[];
  disposers: Array<() => void>;
} {
  const sections: RecordedSection[] = [];
  const disposers: Array<() => void> = [];
  const ctx: FakeCtx = {
    systemPrompt: {
      section(section) {
        sections.push(section);
        const dispose = () => {};
        disposers.push(dispose);
        return dispose;
      },
    },
  };
  return { ctx, sections, disposers };
}

describe('读者契约 prompt-section 插件', () => {
  it('apply 注册且只注册一段：name/order 固定，text 等于契约正文', () => {
    const { ctx, sections } = recordingCtx();

    contractPlugin.apply(ctx);

    expect(sections).toHaveLength(1);
    expect(sections[0]).toEqual({
      name: 'mnemon:reader-contract',
      order: 10250,
      text: READER_CONTRACT_V4,
    });
    // 只传这三个字段：多传字段会被宿主当成未来的 schema 扩展，不是本插件的意图。
    expect(Object.keys(sections[0]).sort()).toEqual(['name', 'order', 'text']);
  });

  it('段名与 persona 段不同名，order 排在 persona suffix（10200）之后', () => {
    expect(READER_CONTRACT_SECTION_NAME).toBe('mnemon:reader-contract');
    expect(READER_CONTRACT_SECTION_NAME).not.toMatch(/^deployment:persona/);
    expect(Number.isFinite(READER_CONTRACT_SECTION_ORDER)).toBe(true);
    expect(READER_CONTRACT_SECTION_ORDER).toBe(10250);
    expect(READER_CONTRACT_SECTION_ORDER).toBeGreaterThan(10200);
  });

  it('inject 只要求 systemPrompt', () => {
    expect(contractPlugin.inject).toEqual(['systemPrompt']);
  });

  it('子路径模块不带 default 导出（主入口的 provider 不能被误当插件挂载）', () => {
    expect('default' in contractPlugin).toBe(false);
  });

  it('apply 不读取 ctx 的其它成员（假 ctx 只有 systemPrompt）', () => {
    const { ctx } = recordingCtx();
    expect(() => contractPlugin.apply(ctx)).not.toThrow();
  });

  it('正文非空且是完整 v4（17 行、含六条规则）', () => {
    expect(READER_CONTRACT_V4.split('\n')).toHaveLength(17);
    expect(READER_CONTRACT_V4).toContain('6. 如果问题是"几次/多少个"这类计数');
    expect(READER_CONTRACT_V4).not.toContain('```');
  });
});
