/** manifest（docs/06 §2/§8）与三处身份一致性。 */

import { describe, expect, it } from 'vitest';

import { MEMORY_SPACE_PROVIDER_API_VERSION } from '../src/contract.js';
import {
  FACTLAYER_CAPABILITIES,
  FACTLAYER_FIELDS,
  FACTLAYER_MANIFEST,
  PROVIDER_ID,
} from '../src/manifest.js';

const MANIFEST_KEYS = [
  'apiVersion',
  'kind',
  'typeId',
  'packageName',
  'version',
  'label',
  'icon',
  'summary',
  'summaryI18nKey',
  'origin',
  'locality',
  'workspaceBinding',
  'capabilities',
  'fields',
  'secrets',
  'scoreSemantics',
].sort();

describe('manifest', () => {
  it('16 字段齐全（含可选 icon / summaryI18nKey）', () => {
    expect(Object.keys(FACTLAYER_MANIFEST).sort()).toEqual(MANIFEST_KEYS);
    expect(Object.keys(FACTLAYER_MANIFEST)).toHaveLength(16);
  });

  it('apiVersion 是官方常量，不是自造字符串', () => {
    expect(FACTLAYER_MANIFEST.apiVersion).toBe(MEMORY_SPACE_PROVIDER_API_VERSION);
    expect(FACTLAYER_MANIFEST.apiVersion).toBe('dsh-mnemon.memory-space-provider/v1');
    expect(FACTLAYER_MANIFEST.kind).toBe('provider');
  });

  it('typeId === 模块 id === factlayer', () => {
    expect(FACTLAYER_MANIFEST.typeId).toBe(PROVIDER_ID);
    expect(PROVIDER_ID).toBe('factlayer');
  });

  it('capabilities 8 个布尔写全 + exact/unsupported', () => {
    expect(Object.keys(FACTLAYER_CAPABILITIES).sort()).toEqual(
      [
        'browse',
        'deletionMode',
        'entities',
        'forget',
        'graph',
        'link',
        'related',
        'remember',
        'search',
        'writeMode',
      ].sort(),
    );
    const booleans = Object.entries(FACTLAYER_CAPABILITIES).filter(
      ([key]) => !['writeMode', 'deletionMode'].includes(key),
    );
    expect(booleans).toHaveLength(8);
    for (const [, value] of booleans) expect(typeof value).toBe('boolean');
    expect(FACTLAYER_CAPABILITIES).toMatchObject({
      search: true,
      browse: false,
      graph: false,
      entities: false,
      related: false,
      remember: true,
      link: false,
      forget: false,
      writeMode: 'exact',
      deletionMode: 'unsupported',
    });
  });

  it('secrets 与 fields 里的 secret 集合完全一致', () => {
    const fromFields = FACTLAYER_FIELDS.filter((field) => field.input === 'secret')
      .map((field) => field.key)
      .sort();
    expect([...FACTLAYER_MANIFEST.secrets].sort()).toEqual(fromFields);
    expect(FACTLAYER_MANIFEST.secrets).toEqual(['apiKey']);
  });

  it('fields 的 scope 符合设计：baseUrl/apiKey 是 service，memoryBodyId 是 memory', () => {
    const byKey = new Map(FACTLAYER_FIELDS.map((field) => [field.key, field]));
    expect(byKey.get('baseUrl')).toMatchObject({ scope: 'service', input: 'url' });
    expect(byKey.get('apiKey')).toMatchObject({ scope: 'service', input: 'secret' });
    expect(byKey.get('memoryBodyId')).toMatchObject({ scope: 'memory', input: 'text' });
    // 只有 memoryBodyId 是 memory 作用域：discover 写 connection 时才合法。
    expect(FACTLAYER_FIELDS.filter((field) => field.scope === 'memory')).toHaveLength(1);
  });

  it('scoreSemantics 与 adapter 声明一致（两处都是 provider-native）', () => {
    expect(FACTLAYER_MANIFEST.scoreSemantics).toBe('provider-native');
  });

  it('origin/locality/workspaceBinding 合法', () => {
    expect(FACTLAYER_MANIFEST.origin).toBe('third-party');
    expect(['local', 'remote']).toContain(FACTLAYER_MANIFEST.locality);
    expect(FACTLAYER_MANIFEST.workspaceBinding).toBe('provider-global');
  });
});
