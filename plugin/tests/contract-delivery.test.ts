/**
 * 「契约随包交付」的清单测试：`package.json` 的 exports/files/dsh.bundle 与
 * `cordis.patch.yml` 必须自洽，否则装进 profile 时是**静默不生效**而不是报错。
 *
 * 这里只做结构断言（不把 yaml 解析器加成依赖）：patch 文件去掉注释与空行后
 * 恰好三行——`- insert:`、`- id:`、`name:`。多出一行（persona/preset/config）
 * 就说明本层超出了「只插一条 prompt-section」的约定。
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

const PACKAGE_PATH = fileURLToPath(new URL('../package.json', import.meta.url));
const PATCH_PATH = fileURLToPath(new URL('../cordis.patch.yml', import.meta.url));

interface ExportEntry {
  types?: string;
  default?: string;
}

interface PackageShape {
  name: string;
  files: string[];
  exports: Record<string, ExportEntry | string>;
  dsh?: { bundle?: { patch?: string | string[] } };
}

function readPackage(): PackageShape {
  return JSON.parse(readFileSync(PACKAGE_PATH, 'utf8')) as PackageShape;
}

function patchCodeLines(): string[] {
  return readFileSync(PATCH_PATH, 'utf8')
    .replace(/^\uFEFF/, '')
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line !== '' && !line.startsWith('#'));
}

describe('契约随包交付：package.json', () => {
  it('包名与 docs/13 的约定一致', () => {
    expect(readPackage().name).toBe('dsh-mnemon-provider-factlayer');
  });

  it('exports 仍以 provider 为主入口，并新增 ./contract 子路径插件', () => {
    const { exports } = readPackage();
    expect(exports['.']).toEqual({
      types: './dist/index.d.ts',
      default: './dist/index.js',
    });
    expect(exports['./contract']).toEqual({
      types: './dist/contract-plugin.d.ts',
      default: './dist/contract-plugin.js',
    });
    // src/contract.ts 是 provider 契约的类型镜像，不是这个插件；别把两者混成一个产物。
    expect(exports['./contract']).not.toEqual({
      types: './dist/contract.d.ts',
      default: './dist/contract.js',
    });
  });

  it('files 覆盖 dist / README / patch / 契约正文（npm 打包后正文随包）', () => {
    const { files } = readPackage();
    expect(files).toEqual(
      expect.arrayContaining([
        'dist',
        'README.md',
        'cordis.patch.yml',
        'contracts',
      ]),
    );
  });

  it('声明 dsh.bundle.patch，使同一个包也能当 profile bundle', () => {
    const { dsh } = readPackage();
    expect(dsh?.bundle?.patch).toBe('./cordis.patch.yml');
  });
});

describe('契约随包交付：cordis.patch.yml', () => {
  it('只插一行全局 prompt-section 插件（不碰 persona、不碰 preset）', () => {
    const lines = patchCodeLines();
    expect(lines).toEqual([
      '- insert:',
      '- id: mnemon-reader-contract',
      'name: dsh-mnemon-provider-factlayer/contract',
    ]);
  });

  it('行名 = <包名>/contract，指向包内子路径导出而非 provider 主入口', () => {
    const { name } = readPackage();
    const lines = patchCodeLines();
    expect(lines[2]).toBe(`name: ${name}/contract`);
    expect(lines[2]).not.toBe(`name: ${name}`);
  });

  it('patch 不含 config/disabled 等改动既有行的字段', () => {
    const raw = readFileSync(PATCH_PATH, 'utf8');
    for (const line of patchCodeLines()) {
      expect(line).not.toMatch(/^config:/);
      expect(line).not.toMatch(/^disabled:/);
      expect(line).not.toMatch(/^- id: (preset|persona)/);
    }
    // 注释里必须写明 minimal preset 的 complete:true 会吞掉本段（docs/13 §1.2）。
    expect(raw).toContain('complete: true');
    expect(raw).toContain('minimal');
  });
});
