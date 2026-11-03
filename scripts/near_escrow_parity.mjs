/** The Python parity driver calls the real TypeScript Arras implementation.
 * Bundle in memory: no install, RPC, generated vectors or tracked-file writes.
 */
import { createRequire } from 'node:module';
import Module from 'node:module';
import { dirname, join } from 'node:path';
import { readFileSync } from 'node:fs';
const tsRoot = process.argv[2];
const req = createRequire(join(tsRoot, 'package.json'));
const source = join(tsRoot, 'src/providers/near/escrow.ts');
const code = req('esbuild').buildSync({ entryPoints: [source], bundle: true,
  platform: 'node', format: 'cjs', write: false }).outputFiles[0].text;
const mod = new Module(source);
mod.paths = Module._nodeModulePaths(dirname(source));
mod._compile(code, source);
const sdk = mod.exports;
const { vectors, cases } = JSON.parse(readFileSync(0, 'utf8'));
const signer = sdk.createNearLocalSigner(Uint8Array.from({ length: 32 }, (_, i) => i));
const results = [];
for (const network of ['near:testnet', 'near:mainnet']) for (const v of vectors.payment_id.vectores) {
  const o = { network, signer, senderId: v.payer,
    escrowContract: sdk.NEAR_ESCROW_CONTRACTS[network][0],
    nonce: '7', maxBlockHeight: '100000000' };
  const authorization = await sdk.signEscrowAuthorization({ ...o, token: sdk.NEAR_ESCROW_TOKENS[network], amount: BigInt(v.amount),
    msg: { ...v, release_deadline: BigInt(v.release_deadline), reclaim_after: BigInt(v.reclaim_after) } });
  const lifecycle = [];
  for (const [method, args] of [
    ['release', { payment_id: v.payment_id }],
    ['release', { payment_id: v.payment_id, amount: '400000' }],
    ['refund', { payment_id: v.payment_id }], ['reclaim', { payment_id: v.payment_id }],
    ['withdraw', {}], ['withdraw_for', { account: v.receiver }],
  ]) lifecycle.push(await sdk.signEscrowLifecycle(o, method, args));
  results.push({ id: v.id, msg: sdk.buildEscrowMsg(v),
    preimage: Buffer.from(sdk.paymentIdPreimage(v)).toString('hex'),
    paymentId: sdk.computePaymentId(v), authorization, lifecycle });
}
const refusals = [];
for (const c of cases) {
  let touched = false;
  const spy = { get publicKey() { touched = true; return signer.publicKey; },
    sign(digest) { touched = true; return signer.sign(digest); } };
  const o = { network: c.network, signer: spy, senderId: c.vector.payer,
    escrowContract: c.contract, nonce: c.nonce, maxBlockHeight: c.height };
  let refused = false;
  try {
    if (c.method === 'authorize') await sdk.signEscrowAuthorization({ ...o,
      token: c.vector.token, amount: c.vector.amount, msg: c.vector });
    else await sdk.signEscrowLifecycle(o, c.method, c.method === 'withdraw_for'
      ? { account: c.vector.receiver } : c.method === 'withdraw' ? {} : { payment_id: c.vector.payment_id });
  } catch { refused = !touched; }
  refusals.push(refused);
}
process.stdout.write(JSON.stringify({ results, contracts: sdk.NEAR_ESCROW_CONTRACTS, refusals,
  minGas: String(sdk.NEAR_ESCROW_MIN_GAS), maxGas: String(sdk.NEAR_ESCROW_MAX_GAS) }));
