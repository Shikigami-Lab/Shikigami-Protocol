// rc2 keeps the required working-directory contribution even when optional
// runtime context is suppressed. Relocate that fact before history projection;
// never remove messages by matching their text (users may quote the same words).
export function installCharacterPrompt(ctx, characterPrompt) {
  ctx.systemPrompt.section({ name: 'shikigami:complete', order: 0, complete: true,
    text: characterPrompt + '\n\n[Execution environment]\nWorking directory: /workspace. '
      + 'This is application context, not dialogue. Relative tool paths start here.',
    interpolate: false });
  ctx.systemPrompt.suppressRuntimeContext();
  // Shadow the required provider so request-admission refresh stays empty.
  ctx.systemPrompt.context({ name: 'working-directory:current',
    order: ctx.systemPrompt.getContextOrder('WORKING_DIRECTORY'),
    required: true, interpolate: false, text: '' });
  // The native working-directory listener also fills this field AFTER next().
  // Wrap it from the outside and remove only its named presentation entry.
  ctx.on('system-prompt/assemble', async (_assembly, _context, next) => {
    const assembly = await next();
    return { ...assembly,
      contexts: assembly.contexts.filter(entry => entry.name !== 'working-directory:current') };
  }, { prepend: true });
}
