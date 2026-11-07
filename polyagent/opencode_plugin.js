// PolyAgent bridge for OpenCode V2's native subagent tool.
// Installed only in a PolyAgent project's .opencode/plugins directory.
const endpoint = __POLYAGENT_ENDPOINT__;

export default {
  id: "polyagent.native-subagent",
  async setup(ctx) {
    await ctx.tool.transform((editor) => {
      editor.update("subagent", (tool) => {
        const nativeExecute = tool.execute;
        tool.execute = async (input, context) => {
          const response = await fetch(endpoint, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ sessionID: context.sessionID, input }),
            signal: context.signal,
          });
          const result = await response.json();
          if (!response.ok) throw new Error(result.error || `PolyAgent bridge HTTP ${response.status}`);
          if (!result.handled) return nativeExecute(input, context);
          const output = {
            sessionID: result.sessionID,
            status: "completed",
            output: result.output,
          };
          return {
            output,
            content: `<subagent sessionID="${output.sessionID}" state="completed">\n${output.output}\n</subagent>`,
            metadata: { sessionID: output.sessionID, status: output.status },
          };
        };
      });
    });
  },
};
