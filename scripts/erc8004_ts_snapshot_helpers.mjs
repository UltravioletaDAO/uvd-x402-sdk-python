// Runtime answers for the ERC-8004 snapshot. Kept pure so its coverage is
// tested offline, without installing a package or rewriting the fixture.
export function networkAnswers(backend, networks, chainToCAIP2) {
  const contracts = Object.entries(backend.ERC8004_CONTRACTS);
  const caip2Names = contracts
    .filter(([, row]) => row.identityRegistry !== undefined)
    .map(([name]) => chainToCAIP2(name));
  const names = [...new Set([...networks, ...contracts.map(([name]) => name), ...caip2Names])];
  const answer = (fn) => Object.fromEntries(names.map((name) => [name, fn(name)]));
  return {
    wireNetwork: answer(backend.wireNetwork),
    supportsRelayedFeedback: answer(backend.supportsRelayedFeedback),
    supportsSolanaFeedback: answer(backend.supportsSolanaFeedback),
  };
}
