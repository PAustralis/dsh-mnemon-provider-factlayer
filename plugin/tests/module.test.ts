/**
 * 装配（docs/06 §1）：
 * - `createFactlayerModule().apply` 必须真的 `host.install` 一份 definition；
 * - `adapter.id === manifest.typeId === module.id`；
 * - 入口在没有装 dsh-mnemon 时必须抛「需要安装 dsh-mnemon」的清晰错误。
 */

import { describe, expect, it } from 'vitest';

import { FACTLAYER_MANIFEST, PROVIDER_ID } from '../src/manifest.js';
import { createFactlayerModule } from '../src/module.js';
import type {
  MemorySpaceProviderDefinition,
  MemorySpaceProviderHost,
  MemorySpaceProviderRuntimeContext,
} from '../src/contract.js';
import { clientFactory, fakeAuthority, jsonResponse, recordingFetch } from './helpers.js';

function installOnce(options: Parameters<typeof createFactlayerModule>[0] = {}) {
  const installed: Array<{
    owner: unknown;
    definition: MemorySpaceProviderDefinition;
  }> = [];
  const host: MemorySpaceProviderHost = {
    install(owner, definition) {
      installed.push({ owner, definition });
      return () => undefined;
    },
  };
  return { installed, host, module: createFactlayerModule(options) };
}

describe('子模块装配', () => {
  it('module.id === manifest.typeId === factlayer', () => {
    const { module } = installOnce();
    expect(module.id).toBe(PROVIDER_ID);
    expect(module.id).toBe(FACTLAYER_MANIFEST.typeId);
  });

  it('apply 调用 host.install(ctx, { manifest, create })', async () => {
    const { installed, host, module } = installOnce();
    const ctx = { id: 'child-context' };

    await module.apply(ctx, host, undefined);

    expect(installed).toHaveLength(1);
    expect(installed[0]?.owner).toBe(ctx);
    expect(installed[0]?.definition.manifest).toBe(FACTLAYER_MANIFEST);
    expect(typeof installed[0]?.definition.create).toBe('function');
  });

  it('create(context) 产出的 adapter.id 与 manifest.typeId 一致；scoreSemantics 两处一致', () => {
    const { installed, host, module } = installOnce();
    module.apply({}, host, undefined);
    const fake = fakeAuthority({ baseUrl: 'http://shim.test' });
    const context: MemorySpaceProviderRuntimeContext = {
      memorySpaces: fake.authority,
      memoryBodies: fake.authority,
      config: { timeoutMs: 15_000 },
      providerInstanceId: PROVIDER_ID,
      manifest: FACTLAYER_MANIFEST,
    };

    const adapter = installed[0]!.definition.create(context);
    expect(adapter.id).toBe(FACTLAYER_MANIFEST.typeId);
    expect(adapter.scoreSemantics).toEqual({
      kind: FACTLAYER_MANIFEST.scoreSemantics,
    });
  });

  it('create 出来的 adapter 能真的发请求（注入的客户端直达 shim 形状）', async () => {
    const recorder = recordingFetch(() =>
      jsonResponse([{ id: 'factlayer-default', name: 'factlayer' }]),
    );
    const { installed, host, module } = installOnce({
      createClient: clientFactory(recorder.fetch),
    });
    module.apply({}, host, undefined);
    const fake = fakeAuthority({ baseUrl: 'http://shim.test' });
    const adapter = installed[0]!.definition.create({
      memorySpaces: fake.authority,
      memoryBodies: fake.authority,
      config: { timeoutMs: 15_000 },
      providerInstanceId: PROVIDER_ID,
      manifest: FACTLAYER_MANIFEST,
    });

    const spaces = await adapter.discover!({ baseUrl: 'http://shim.test' });
    expect(spaces[0]?.externalId).toBe('factlayer-default');
  });
});

describe('入口（index.ts）', () => {
  it('没有装 dsh-mnemon 时，import 以「需要安装 dsh-mnemon」失败', async () => {
    // 本仓库按任务要求不安装 peer（见 plugin/.npmrc + pnpm-workspace.yaml）。
    await expect(import('../src/index.js')).rejects.toThrow(/需要安装 dsh-mnemon/u);
  });
});
