/**
 * 装配（docs/06 §1）：
 * - `createFactlayerModule().apply` 必须真的 `host.install` 一份 definition；
 * - `adapter.id === manifest.typeId === module.id`；
 * - 入口在 SDK 不可解析时必须以「需要安装 dsh-mnemon」失败（错误分支用注入的
 *   importer 真测），而在 peer 可解析时必须真的装配成功（真实环境断言）。
 */

import { describe, expect, it } from 'vitest';

import { FACTLAYER_MANIFEST, PROVIDER_ID } from '../src/manifest.js';
import { createFactlayerModule } from '../src/module.js';
import { loadEntryModule } from '../src/entry.js';
import {
  loadDefineMemorySpaceProvider,
  ProviderSdkUnavailableError,
} from '../src/define.js';
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
  it('SDK 不可解析时以「需要安装 dsh-mnemon」失败；peer 可解析时装配成功且 id 正确', async () => {
    // 错误分支（依赖注入真测）：本仓库为让本包在 profile 里解析 peer，故意保留
    // node_modules/dsh-mnemon* 两个 junction（生产需要），入口的裸动态 import 永远
    // 成功，无法用它触发失败。这里注入一个「任何 specifier 都解析失败」的 importer，
    // 走的仍是 loadEntryModule → loadDefineMemorySpaceProvider → 抛错的真实路径。
    const resolveFailure = (specifier: string) =>
      Promise.reject(
        Object.assign(new Error(`Cannot find package '${specifier}'`), {
          code: 'ERR_MODULE_NOT_FOUND',
        }),
      );

    const rejection: unknown = await loadEntryModule(resolveFailure).catch(
      (error: unknown) => error,
    );
    expect(rejection).toBeInstanceOf(ProviderSdkUnavailableError);
    const sdkError = rejection as ProviderSdkUnavailableError;
    expect(sdkError.message).toContain('需要安装 dsh-mnemon');
    // 两个候选都被尝试并按顺序记录，错误信息才真的能指引用户。
    expect(sdkError.attempts).toEqual([
      expect.stringMatching(/^dsh-mnemon-source-memory-spaces\/provider-sdk：Cannot find package/u),
      expect.stringMatching(/^dsh-mnemon：Cannot find package/u),
    ]);

    // 成功分支（真实环境）：探测生产解析路径；peer 在时入口必须真的完成 SDK 包装。
    const peerResolvable = await loadDefineMemorySpaceProvider().then(
      () => true,
      () => false,
    );
    if (peerResolvable) {
      const entry = await import('../src/index.js');
      expect(entry.default.id).toBe(PROVIDER_ID);
      expect(entry.default.id).toBe(FACTLAYER_MANIFEST.typeId);
    } else {
      // 干净环境（CI 未建 peer junction）：入口仍以同一指引失败，而不是崩在别处。
      await expect(import('../src/index.js')).rejects.toThrow(/需要安装 dsh-mnemon/u);
    }
  });
});
