#!/usr/bin/env node
import { access, stat } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const DEFAULT_GENERATOR_ROOT = resolve(
  process.env.HOME,
  'git/duckdb-monday/search-engine/data-generator',
);
const DEFAULT_OUTPUT_DIR = resolve('benchmark-results/datagen');

function usage() {
  console.error(`Usage:
  node scripts/generate_datagen_dataset.mjs --account-sizes 100000 [--output-dir benchmark-results/datagen-100k]
  node scripts/generate_datagen_dataset.mjs --scale-factor 0.01 [--output-dir benchmark-results/datagen-sf-0.01]

Options:
  --generator-root <path>   data-generator checkout root
  --output-dir <path>       output directory for items.jsonl, accounts.jsonl, boards.jsonl, manifest.json
  --seed <n>                deterministic seed, default 42
  --workers <n>             parallel workers, default 4
  --account-sizes <csv>     explicit tenant item counts, e.g. 100000,500000,1000000
  --start-account-id <n>    first account id for explicit account sizes, default 900001
  --scale-factor <n>        use the generator's native scale-factor account distribution
`);
}

function readArgs(argv) {
  const args = {
    generatorRoot: DEFAULT_GENERATOR_ROOT,
    outputDir: DEFAULT_OUTPUT_DIR,
    seed: 42,
    workers: 4,
    accountSizes: null,
    startAccountId: 900001,
    scaleFactor: null,
  };

  for (let i = 2; i < argv.length; i++) {
    const arg = argv[i];
    const next = () => {
      i += 1;
      if (i >= argv.length) {
        throw new Error(`Missing value for ${arg}`);
      }
      return argv[i];
    };

    if (arg === '--generator-root') args.generatorRoot = resolve(next());
    else if (arg === '--output-dir') args.outputDir = resolve(next());
    else if (arg === '--seed') args.seed = Number(next());
    else if (arg === '--workers') args.workers = Number(next());
    else if (arg === '--account-sizes') args.accountSizes = next();
    else if (arg === '--start-account-id') args.startAccountId = Number(next());
    else if (arg === '--scale-factor') args.scaleFactor = Number(next());
    else if (arg === '--help' || arg === '-h') {
      usage();
      process.exit(0);
    } else {
      throw new Error(`Unknown argument: ${arg}`);
    }
  }

  if (!Number.isInteger(args.seed)) throw new Error('--seed must be an integer');
  if (!Number.isInteger(args.workers) || args.workers < 1) throw new Error('--workers must be a positive integer');
  if (args.scaleFactor !== null && (!Number.isFinite(args.scaleFactor) || args.scaleFactor < 0)) {
    throw new Error('--scale-factor must be a non-negative number');
  }
  if (args.accountSizes !== null && args.scaleFactor !== null) {
    throw new Error('Use either --account-sizes or --scale-factor, not both');
  }
  if (args.accountSizes === null && args.scaleFactor === null) {
    throw new Error('Provide --account-sizes or --scale-factor');
  }

  return args;
}

function parseAccountSizes(value, startAccountId) {
  if (!Number.isInteger(startAccountId) || startAccountId < 1) {
    throw new Error('--start-account-id must be a positive integer');
  }
  return value.split(',').map((raw, index) => {
    const totalItems = Number(raw.trim());
    if (!Number.isInteger(totalItems) || totalItems < 1) {
      throw new Error(`Invalid account size: ${raw}`);
    }
    return {
      account_id: startAccountId + index,
      workspace_count: 1,
      board_count: 0,
      total_items: totalItems,
    };
  });
}

async function importCore(generatorRoot) {
  const distIndex = join(generatorRoot, 'packages/core/dist/index.js');
  try {
    await access(distIndex);
  } catch {
    throw new Error(
      `Generator core is not built at ${distIndex}. Run "yarn install" and "yarn build" in ${generatorRoot}.`,
    );
  }
  return import(pathToFileURL(distIndex).href);
}

function formatBytes(bytes) {
  const units = ['B', 'KiB', 'MiB', 'GiB'];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

try {
  const args = readArgs(process.argv);
  const core = await importCore(args.generatorRoot);
  const startTime = Date.now();
  let latestItems = 0;
  const accounts = args.accountSizes === null
    ? undefined
    : parseAccountSizes(args.accountSizes, args.startAccountId);

  const progress = setInterval(() => {
    console.log(`items=${latestItems.toLocaleString()}`);
  }, 10000);

  try {
    const result = await core.generateWithMetadataParallel({
      seed: args.seed,
      scaleFactor: args.scaleFactor ?? 0,
      outputDir: args.outputDir,
      workers: args.workers,
      accounts,
      onProgress: p => {
        latestItems = p.itemsGenerated;
      },
    });

    await core.writeAccountsJsonl(result.accounts, { outputDir: args.outputDir });
    await core.writeBoardsJsonl(result.boards, { outputDir: args.outputDir });

    const durationSeconds = (Date.now() - startTime) / 1000;
    await core.writeManifest(args.outputDir, {
      seed: args.seed,
      scale_factor: args.scaleFactor ?? 0,
      datagen_version: core.defaults.DATAGEN_VERSION,
      item_count: result.itemCount,
      account_count: result.accountIds.size,
      board_count: result.boardIds.size,
      generated_at: new Date().toISOString(),
      duration_seconds: Math.round(durationSeconds * 100) / 100,
    });

    const itemsPath = join(args.outputDir, 'items.jsonl');
    const itemsStat = await stat(itemsPath);
    const tenantLabels = [...result.accountIds].sort((a, b) => a - b).map(id => `account_${id}`);

    console.log('');
    console.log(`Items:    ${result.itemCount.toLocaleString()}`);
    console.log(`Accounts: ${result.accountIds.size.toLocaleString()} (${tenantLabels.join(', ')})`);
    console.log(`Boards:   ${result.boardIds.size.toLocaleString()}`);
    console.log(`Size:     ${formatBytes(itemsStat.size)}`);
    console.log(`Duration: ${durationSeconds.toFixed(2)}s`);
    console.log(`Output:   ${args.outputDir}`);
  } finally {
    clearInterval(progress);
  }
} catch (err) {
  console.error(err instanceof Error ? err.message : String(err));
  usage();
  process.exit(1);
}
