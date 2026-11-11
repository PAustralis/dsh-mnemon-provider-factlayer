/**
 * 子模块与 adapter 的装配（docs/06 §1）。
 *
 * 拆出本文件而不是全写在 `index.ts`：`index.ts` 用顶层 await 动态 import
 * Provider SDK（peer 未安装时 import 会拒绝），单测需要能只 import 装配逻辑
 * 而不触发那次 import。
 *
 * 硬约束：`module.id === manifest.typeId === adapter.id`（三处逐字一致）。
 */

import { FactlayerAdapter, type FactlayerAdapterOptions } from './adapter.js';
import { ConnectionCache } from './config.js';
import { FACTLAYER_MANIFEST, PROVIDER_ID } from './manifest.js';
import type {
  MemoryProviderAdapter,
  MemorySpaceProviderModule,
  MemorySpaceProviderRuntimeContext,
} from './contract.js';

export interface FactlayerModuleOptions {
  /** 测试注入点：造 HTTP 客户端。 */
  createClient?: FactlayerAdapterOptions['createClient'];
  /** 测试注入点：时钟。 */
  now?: () => number;
}

/** `create(context)`：Source 每次装配 generation 时调用一次。 */
export function createFactlayerAdapter(
  context: MemorySpaceProviderRuntimeContext,
  options: FactlayerModuleOptions = {},
): MemoryProviderAdapter {
  const providerId =
    typeof context.providerInstanceId === 'string' && context.providerInstanceId !== ''
      ? context.providerInstanceId
      : PROVIDER_ID;
  const connections = new ConnectionCache(context, providerId);
  return new FactlayerAdapter({
    connections,
    ...(options.createClient === undefined ? {} : { createClient: options.createClient }),
    ...(options.now === undefined ? {} : { now: options.now }),
  });
}

/**
 * typed child module：`apply` 内必须真的 `host.install` 一份 definition，
 * 否则装配直接失败（"Memory Space Provider child did not install a definition"）。
 */
export function createFactlayerModule(
  options: FactlayerModuleOptions = {},
): MemorySpaceProviderModule {
  return {
    id: PROVIDER_ID,
    apply(ctx, host) {
      host.install(ctx, {
        manifest: FACTLAYER_MANIFEST,
        create: (context) => createFactlayerAdapter(context, options),
      });
    },
  };
}
