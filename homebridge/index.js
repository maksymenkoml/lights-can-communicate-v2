import { LightsCanCommunicatePlatform, PLATFORM_NAME, PLUGIN_NAME } from './lib/platform.js';

export default (api) => {
  api.registerPlatform(PLUGIN_NAME, PLATFORM_NAME, LightsCanCommunicatePlatform);
};
