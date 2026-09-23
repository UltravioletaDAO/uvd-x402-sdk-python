#!/usr/bin/env node
// Snapshot the ERC-8004 network lists of the PUBLISHED TypeScript SDK into
// tests/fixtures/erc8004-ts.json, which tests/test_erc8004_ts_parity.py
// compares against this package.
//
// The lists are read from what npm serves, not from a checkout of the
// TypeScript repo: a consumer gets the published package, and a local branch
// can hold lists that were never released.
//
//   node scripts/erc8004_ts_snapshot.mjs                    # write the fixture (uvd-x402-sdk@2.98.0)
//   node scripts/erc8004_ts_snapshot.mjs --version 2.99.0   # snapshot another release
//   node scripts/erc8004_ts_snapshot.mjs --check            # exit 1 if the committed fixture
//                                                           # differs from the release it names
//
// Needs Node >= 18 and npm on PATH. Installs from registry.npmjs.org into a
// temporary directory with --ignore-scripts and removes it afterwards; nothing
// in this repo is touched except the fixture (and only without --check).
//
// What goes into the fixture:
// - erc8004Network: the `Erc8004Network` union, parsed from the published
//   .d.ts (a type does not exist at runtime).
// - erc8004Contracts, relayedFeedbackNetworks, solanaFeedbackNetworks: the
//   exported values of `uvd-x402-sdk/backend`, as loaded by Node.
// - wireNetwork, supportsRelayedFeedback, supportsSolanaFeedback: what the
//   exported functions answer for every network name above, so the Python
//   helpers are compared on behaviour, not only on list contents.

import { execFileSync } from 'node:child_process';
import {
  mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync, writeFileSync,
} from 'node:fs';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const PACKAGE = 'uvd-x402-sdk';
const DEFAULT_VERSION = '2.98.0';
const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const FIXTURE = join(REPO_ROOT, 'tests', 'fixtures', 'erc8004-ts.json');

function readFixture() {
  try {
    return readFileSync(FIXTURE, 'utf8');
  } catch {
    return '';
  }
}

function parseArgs(argv) {
  const args = { version: undefined, check: false };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--check') args.check = true;
    else if (argv[i] === '--version') args.version = argv[++i];
    else throw new Error(`unknown argument: ${argv[i]}`);
  }
  // --check compares against the release the committed fixture names, so a
  // fixture moved to a newer release does not need the flag repeated.
  if (args.version === undefined) {
    args.version = args.check
      ? (JSON.parse(readFixture() || '{}').source?.version ?? DEFAULT_VERSION)
      : DEFAULT_VERSION;
  }
  if (!/^\d+\.\d+\.\d+$/.test(args.version ?? '')) {
    throw new Error(`--version needs an exact release (x.y.z), got ${args.version}`);
  }
  return args;
}

function install(version, dir) {
  execFileSync(
    'npm',
    ['install', '--prefix', dir, '--registry', 'https://registry.npmjs.org/', '--no-save',
      '--ignore-scripts', '--no-audit', '--no-fund', `${PACKAGE}@${version}`],
    { stdio: ['ignore', 'ignore', 'inherit'], shell: process.platform === 'win32' },
  );
  const lock = JSON.parse(readFileSync(join(dir, 'node_modules', '.package-lock.json'), 'utf8'));
  const entry = lock.packages[`node_modules/${PACKAGE}`];
  if (entry?.version !== version) {
    throw new Error(`npm installed ${PACKAGE}@${entry?.version}, expected ${version}`);
  }
  return { resolved: entry.resolved, integrity: entry.integrity };
}

function declarationFiles(dir) {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const path = join(dir, e.name);
    if (e.isDirectory()) return declarationFiles(path);
    return /\.d\.m?ts$/.test(e.name) ? [path] : [];
  });
}

// The union is emitted into a hashed chunk (dist/index-<hash>.d.ts and .d.mts),
// so every declaration file is searched and all of them must agree.
function readNetworkUnion(pkgDir) {
  const unions = new Set();
  for (const file of declarationFiles(join(pkgDir, 'dist'))) {
    for (const m of readFileSync(file, 'utf8').matchAll(/type Erc8004Network\s*=\s*([^;]+);/g)) {
      unions.add(JSON.stringify([...m[1].matchAll(/'([^']+)'/g)].map((n) => n[1])));
    }
  }
  if (unions.size !== 1) {
    throw new Error(`expected one Erc8004Network declaration, found ${unions.size}`);
  }
  return JSON.parse([...unions][0]);
}

function snapshot(version) {
  // realpath: on macOS the temp dir sits behind the /var -> /private/var
  // symlink, and npm keys its lockfile by the resolved path.
  const dir = realpathSync(mkdtempSync(join(tmpdir(), 'erc8004-ts-')));
  try {
    const source = install(version, dir);
    const pkgDir = join(dir, 'node_modules', PACKAGE);
    const backend = createRequire(join(dir, 'index.js'))(`${PACKAGE}/backend`);
    const networks = readNetworkUnion(pkgDir);
    const names = [...new Set([...networks, ...Object.keys(backend.ERC8004_CONTRACTS)])];
    const answer = (fn) => Object.fromEntries(names.map((n) => [n, fn(n)]));
    return {
      generatedBy: `node scripts/erc8004_ts_snapshot.mjs --version ${version}`,
      source: { package: PACKAGE, version, ...source },
      erc8004Network: networks,
      erc8004Contracts: backend.ERC8004_CONTRACTS,
      relayedFeedbackNetworks: [...backend.RELAYED_FEEDBACK_NETWORKS],
      solanaFeedbackNetworks: [...backend.SOLANA_FEEDBACK_NETWORKS],
      wireNetwork: answer(backend.wireNetwork),
      supportsRelayedFeedback: answer(backend.supportsRelayedFeedback),
      supportsSolanaFeedback: answer(backend.supportsSolanaFeedback),
    };
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

const args = parseArgs(process.argv.slice(2));
const text = `${JSON.stringify(snapshot(args.version), null, 2)}\n`;
if (args.check) {
  // A missing fixture reads as '' and is reported as a difference.
  if (readFixture() !== text) {
    console.error(`${FIXTURE} differs from ${PACKAGE}@${args.version} as published.`);
    console.error(`Regenerate it with: node scripts/erc8004_ts_snapshot.mjs --version ${args.version}`);
    process.exit(1);
  }
  console.log(`OK: ${FIXTURE} matches ${PACKAGE}@${args.version} as published.`);
} else {
  writeFileSync(FIXTURE, text);
  console.log(`wrote ${FIXTURE} from ${PACKAGE}@${args.version}`);
}
