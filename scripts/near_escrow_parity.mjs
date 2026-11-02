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
const vectors = JSON.parse(readFileSync(0, 'utf8'));
const signer = sdk.createNearLocalSigner(Uint8Array.from({ length: 32 }, (_, i) => i));
const results = [];
for (const v of vectors.payment_id.vectores) {
  const network = v.token === sdk.NEAR_ESCROW_TOKENS['near:mainnet'] ? 'near:mainnet' : 'near:testnet';
  const o = { network, signer, senderId: v.payer,
    escrowContract: network === 'near:mainnet' ? 'arras.near' : 'arras.testnet',
    nonce: '7', maxBlockHeight: '100000000' };
  const authorization = await sdk.signEscrowAuthorization({ ...o, token: v.token, amount: v.amount, msg: v });
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
process.stdout.write(JSON.stringify({ results,
  minGas: String(sdk.NEAR_ESCROW_MIN_GAS), maxGas: String(sdk.NEAR_ESCROW_MAX_GAS) }));
