import { defineConfig, lazyPlugins } from 'vite-plus';

export default defineConfig({
  // Standard Vite configuration for dev/build/preview.
  plugins: lazyPlugins(() => []),

  // Vitest configuration.
  test: {
    include: ['src/**/*.test.ts'],
  },

  // Oxlint configuration.
  lint: {
    ignorePatterns: ['dist/**'],
  },

  // Oxfmt configuration.
  fmt: {
    semi: true,
    singleQuote: true,
  },

  // Vite Task configuration.
  run: {
    tasks: {
      'generate:icons': {
        command: 'node scripts/generate-icons.js',
        env: ['ICON_THEME'],
      },
    },
  },

  // `vp staged` configuration.
  staged: {
    '*': 'vp check --fix',
  },
});
