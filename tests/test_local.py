import tempfile
import unittest
import json
import time
from unittest.mock import Mock, patch
from pathlib import Path

from polyagent.app import PolyAgent, distinct_model_labels, first_subagent_message
from polyagent.deepseek import DeepSeekClient
from polyagent.codex import CodexClient
from polyagent.antigravity import AntigravityClient
from server import Handler


class LocalBehavior(unittest.TestCase):
    def test_antigravity_progress_includes_command_and_file(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PolyAgent(Path(directory))
            project = app.create_project("Progress")
            conversation = app.create_conversation(project["id"])
            app._execute("INSERT INTO chats(id,project_id,role,harness,model,thread_id,status,created_at,conversation_id) VALUES(?,?,?,?,?,?,?,?,?)",
                         ("chat", project["id"], "orchestrator", "antigravity", "model", "native", "running", time.time(), conversation["id"]))
            app.antigravity = Mock()
            app.antigravity.trajectory.return_value = {"status": "CASCADE_RUN_STATUS_IDLE", "trajectory": {"steps": [
                {"type": "CORTEX_STEP_TYPE_RUN_COMMAND", "status": "CORTEX_STEP_STATUS_DONE", "runCommand": {"commandLine": "pwd", "exitCode": 0}},
                {"type": "CORTEX_STEP_TYPE_CODE_ACTION", "status": "CORTEX_STEP_STATUS_DONE", "codeAction": {"actionSpec": {"createFile": {"path": {"absoluteUri": "file:///project/index.html"}}}}},
                {"type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE", "status": "CORTEX_STEP_STATUS_DONE", "plannerResponse": {"modifiedResponse": "Done"}}]}}
            app._watch_antigravity(project["id"], "chat", "native", "model")
            tools = [e for e in app.events(project["id"]) if e["kind"] == "tool"]
            self.assertEqual(tools[0]["metadata"]["detail"], {"command": "pwd", "exit_code": 0})
            self.assertEqual(tools[1]["metadata"]["detail"]["file"], "file:///project/index.html")
            app.db.close()

    def test_open_project_file_stays_in_project_and_avoids_executables(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            (root / "index.html").write_text("<h1>Preview</h1>")
            (root / "program.sh").write_text("echo test")
            (Path(outside) / "secret.txt").write_text("secret")
            (root / "link.txt").symlink_to(Path(outside) / "secret.txt")
            handler = Handler.__new__(Handler)
            handler.app = Mock()
            handler.app.project.return_value = {"path": directory}
            with patch("server.subprocess.Popen") as launch:
                self.assertEqual(handler._open_project_item("project", "index.html"), {"opened": True})
                self.assertEqual(Path(launch.call_args.args[0][-1]), (root / "index.html").resolve())
                for target in (str(Path(outside) / "secret.txt"), "link.txt", "program.sh"):
                    with self.assertRaises(ValueError):
                        handler._open_project_item("project", target)
                self.assertEqual(launch.call_count, 1)
    def test_interrupt_uses_native_actions_and_keeps_late_results_out(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PolyAgent(Path(directory))
            project = app.create_project("Interruptions")
            conversation = app.create_conversation(project["id"])
            for chat_id, role, harness in (("orch", "orchestrator", "codex"),
                                           ("child", "subagent", "deepseek_harness")):
                app._execute("INSERT INTO chats(id,project_id,role,harness,model,thread_id,status,created_at,conversation_id) VALUES(?,?,?,?,?,?,?,?,?)",
                             (chat_id, project["id"], role, harness, "model", chat_id + "-native",
                              "running", time.time(), conversation["id"]))
            app.codex.interrupt_turn = Mock()
            app.deepseek = Mock()
            app.active_turns["orch"] = "turn-id"
            result = app.interrupt_all(project["id"], conversation["id"])
            self.assertEqual(result["errors"], [])
            self.assertEqual([item["chat_id"] for item in result["results"]], ["child", "orch"])
            app.codex.interrupt_turn.assert_called_once_with("orch-native", "turn-id")
            app.deepseek.interrupt.assert_called_once_with("child-native")
            self.assertEqual([c["status"] for c in app.chats(project["id"])], ["interrupted", "interrupted"])
            app._finish_chat(app._one("SELECT * FROM chats WHERE id='child'"), "completed", answer="late answer")
            self.assertFalse(any(e["kind"] == "assistant" for e in app.events(project["id"])))
            with self.assertRaisesRegex(ValueError, "Chat not found"):
                app.interrupt_chat(project["id"], "outside")

    def test_native_antigravity_policies_are_independent(self):
        client = AntigravityClient("/tmp/Antigravity.app")
        calls = []
        client.call = lambda method, params: calls.append(params)
        client.send("chat", "message", {"model": "native"},
                    {"approval": "request-review", "artifact_review": "harness"})
        tool = calls[-1]["cascadeConfig"]["plannerConfig"]["toolConfig"]
        self.assertEqual(tool, {"runCommand": {"autoCommandConfig": {"autoExecutionPolicy": 1}}})
        client.send("chat", "message", {"model": "native"},
                    {"approval": "harness", "artifact_review": "always-proceed"})
        self.assertEqual(calls[-1]["cascadeConfig"]["plannerConfig"]["toolConfig"],
                         {"notifyUser": {"artifactReviewMode": 2}})
        client.send("chat", "message", {"model": "native"}, {})
        self.assertNotIn("toolConfig", calls[-1]["cascadeConfig"]["plannerConfig"])

    def test_antigravity_interrupt_uses_current_app_action(self):
        client = AntigravityClient("/tmp/Antigravity.app")
        client.call = Mock(return_value={})
        client.interrupt("cascade-id")
        client.call.assert_called_once_with("CancelCascadeInvocation", {"cascadeId": "cascade-id"}, timeout=15)

    def test_obsolete_codex_approval_is_not_offered_or_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PolyAgent(Path(directory))
            app._execute("INSERT INTO harness_preferences(harness,settings) VALUES(?,?)",
                         ("codex", json.dumps({"approval": "on-failure", "sandbox": "workspace-write"})))
            modes = app.harness_modes()["codex"]
            self.assertNotIn("on-failure", {choice["value"] for choice in modes["approval"]})
            self.assertEqual(modes["last_used"], {"sandbox": "workspace-write"})

    def test_settings_follow_native_transport_and_preserve_legacy_choices(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PolyAgent(Path(directory))
            app.save_execution_settings({}, "antigravity", {"approval": "always-proceed"})
            self.assertEqual(app.harness_modes()["antigravity"]["last_used"]["artifact_review"], "always-proceed")
            app.antigravity_source = "cli"
            capability = app.harness_modes()["antigravity"]
            self.assertNotIn("artifact_review", capability)
            with self.assertRaisesRegex(ValueError, "Unknown harness setting"):
                app.save_execution_settings({}, "antigravity", {"artifact_review": "auto"})
            app.save_execution_settings({}, "antigravity", {"approval": "strict", "execution": "accept-edits"})
            app.antigravity_source = "app"
            self.assertEqual(app.execution_settings({}, "antigravity"), {})
            with self.assertRaisesRegex(ValueError, "Unsupported approval"):
                app.save_execution_settings({}, "codex", {"approval": "always-proceed"})

    def test_auto_selection_does_not_receive_other_harness_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PolyAgent(Path(directory))
            project = app.create_project("Auto modes")
            conversation = app.create_conversation(project["id"])
            scoped = {**project, "_conversation_id": conversation["id"]}
            app.save_execution_settings(scoped, "codex", {"approval": "untrusted"})
            def select_other_harness(project_id, message, conversation_id, settings):
                self.assertIsNone(settings)
                return app.execution_settings(scoped, "codex")
            app._send = select_other_harness
            result = app.send(project["id"], "task", conversation["id"],
                              {"approval": "always-proceed", "artifact_review": "request-review"}, "antigravity")
            self.assertEqual(result, {"approval": "untrusted"})
            self.assertEqual(app.execution_settings(scoped, "antigravity")["artifact_review"], "request-review")

    def test_restart_keeps_chat_resumable_and_expires_old_runtime_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PolyAgent(Path(directory))
            project = app.create_project("Recovery")
            conversation = app.create_conversation(project["id"])
            app._execute("INSERT INTO chats(id,project_id,role,harness,model,thread_id,status,created_at,conversation_id) VALUES(?,?,?,?,?,?,?,?,?)",
                ("running", project["id"], "orchestrator", "codex", "model", "native", "running", time.time(), conversation["id"]))
            app.event(project["id"], "running", "approval", "Pending command", {"request_id": "old-request"})
            app.db.close()
            reopened = PolyAgent(Path(directory))
            self.assertEqual(reopened.chats(project["id"])[0]["status"], "interrupted")
            self.assertEqual(reopened.chats(project["id"])[0]["thread_id"], "native")
            self.assertTrue(any(e["kind"] == "request_resolved" and e["metadata"]["request_id"] == "old-request"
                                for e in reopened.events(project["id"])))
    def test_project_chats_are_lazy_independent_and_persistent(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PolyAgent(Path(directory))
            project = app.create_project("Multiple chats")
            first = app.create_conversation(project["id"])
            second = app.create_conversation(project["id"])
            self.assertNotEqual(first["id"], second["id"])
            self.assertEqual(app.chats(project["id"]), [])
            self.assertIsNone(app.codex.process)
            for i, conversation in enumerate((first, second)):
                chat_id = f"native-chat-{i}"
                app._execute("INSERT INTO chats(id,project_id,role,harness,model,thread_id,status,created_at,conversation_id) VALUES(?,?,?,?,?,?,?,?,?)",
                    (chat_id, project["id"], "orchestrator", "codex", "model", f"thread-{i}", "idle", time.time(), conversation["id"]))
                app.event(project["id"], chat_id, "user", f"message-{i}")
            events = app.events(project["id"])
            self.assertEqual([e["conversation_id"] for e in events], [first["id"], second["id"]])
            app.db.close()
            reopened = PolyAgent(Path(directory))
            self.assertEqual(len(reopened.conversations(project["id"])), 2)

    def test_chat_settings_remember_last_choice_and_keep_existing_chat_choice(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PolyAgent(Path(directory))
            project = app.create_project("Modes")
            first = app.create_conversation(project["id"])
            first_project = {**project, "_conversation_id": first["id"]}
            app.save_execution_settings(first_project, "codex", {"approval": "on-request", "sandbox": "read-only"})
            second = app.create_conversation(project["id"])
            second_project = {**project, "_conversation_id": second["id"]}
            self.assertEqual(app.execution_settings(second_project, "codex")["approval"], "on-request")
            app.save_execution_settings(second_project, "codex", {"approval": "never"})
            self.assertEqual(app.execution_settings(first_project, "codex")["approval"], "on-request")
            app.db.close()
            reopened = PolyAgent(Path(directory))
            self.assertEqual(reopened.harness_modes()["codex"]["last_used"], {"approval": "never"})

    def test_chat_from_other_project_cannot_be_sent_to(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PolyAgent(Path(directory))
            one = app.create_project("One")
            two = app.create_project("Two")
            chat = app.create_conversation(one["id"])
            with self.assertRaisesRegex(ValueError, "does not belong"):
                app.send(two["id"], "hello", chat["id"])

    def test_legacy_project_history_is_preserved_as_one_chat(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PolyAgent(Path(directory))
            project = app.create_project("Legacy")
            app._execute("INSERT INTO chats(id,project_id,role,harness,model,thread_id,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                ("legacy", project["id"], "orchestrator", "codex", "model", "thread", "idle", time.time()))
            app.event(project["id"], "legacy", "user", "preserved text")
            app.db.close()
            reopened = PolyAgent(Path(directory))
            conversation = reopened.conversations(project["id"])[0]
            event = reopened.events(project["id"])[0]
            self.assertEqual(event["text"], "preserved text")
            self.assertEqual(event["conversation_id"], conversation["id"])

    def test_native_defaults_do_not_force_codex_permission_overrides(self):
        client = CodexClient(lambda event: None)
        calls = []
        def capture(method, params):
            calls.append((method, params))
            return {"thread": {"id": "native"}}
        client.call = capture
        client.start_thread("/tmp", "model", settings={"approval": "harness", "sandbox": "harness"})
        self.assertNotIn("approvalPolicy", calls[0][1])
        self.assertNotIn("sandbox", calls[0][1])

    def test_deepseek_acp_catalog_uses_exact_provider_model_routes(self):
        options = [{"id": "model", "options": [{"group": "deepseek-official", "options": [
            {"value": '["deepseek-official","deepseek-v4-pro"]', "name": "DeepSeek V4 Pro"},
            {"value": '["deepseek-official","deepseek-flash"]', "name": "DeepSeek Flash"}]}]}]
        catalog = DeepSeekClient.catalog(options)
        self.assertEqual(set(catalog), {"deepseek-official/deepseek-v4-pro", "deepseek-official/deepseek-flash"})
        self.assertEqual(catalog["deepseek-official/deepseek-v4-pro"]["value"],
                         '["deepseek-official","deepseek-v4-pro"]')

    def test_grok_hook_ignores_ordinary_tool_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PolyAgent(Path(directory))
            self.assertEqual(app.handle_grok_hook({"hookEventName": "PreToolUse",
                                                   "toolName": "run_terminal_cmd",
                                                   "toolInput": {"command": "pwd"}}),
                             {"decision": "allow"})

    def test_distinct_catalog_labels_preserve_native_model_identity(self):
        names = {"gemini-3.1-flash-lite": "Gemini 3.5 Flash Lite",
                 "gemini-3.5-flash-lite": "Gemini 3.5 Flash Lite",
                 "gemini-2.5-pro": "Gemini 2.5 Pro"}
        labels = distinct_model_labels(names, lambda model_id: model_id.replace("-", " ").title())
        self.assertEqual(labels["gemini-3.1-flash-lite"], "Gemini 3.1 Flash Lite")
        self.assertEqual(labels["gemini-3.5-flash-lite"], "Gemini 3.5 Flash Lite")
        self.assertEqual(len({name.casefold() for name in labels.values()}), len(names))

    def test_project_creation_is_lazy_and_persistent(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PolyAgent(Path(directory))
            project = app.create_project("Sample")
            self.assertTrue(Path(project["path"]).is_dir())
            self.assertEqual(app.chats(project["id"]), [])
            app.db.close()
            reopened = PolyAgent(Path(directory))
            self.assertEqual(reopened.project(project["id"])["name"], "Sample")

    def test_first_message_and_followups_have_distinct_forms(self):
        prompt = "Implement authentication and test it."
        self.assertEqual(first_subagent_message(prompt),
                         "you are a subagent working under a ai orchestrator. all messages excluding these sentences are from orchestrator. orchestrator says: 'Implement authentication and test it.'")

    def test_rejects_application_directory_as_project(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PolyAgent(Path(directory))
            with self.assertRaises(ValueError):
                app.create_project("Bad", str(Path(directory) / "data" / "child"))

    def test_research_updates_preserve_user_override(self):
        with tempfile.TemporaryDirectory() as directory:
            app = PolyAgent(Path(directory))
            app._execute("INSERT INTO models(id,harness,display_name,metadata,discovered_at) VALUES(?,?,?,?,?)",
                         ("model-test", "codex", "Model Test", json.dumps({"description": ""}), time.time()))
            app.set_override("model-test", {"strengths": "Good at tests", "weaknesses": "Slow", "assessment": "Preferred", "preference": "prefer"})
            app._execute("INSERT INTO research(model_id,strengths,weaknesses,evidence,conclusion,updated_at) VALUES(?,?,?,?,?,?)",
                         ("model-test", json.dumps(["Research strength"]), json.dumps([]), json.dumps([]), "New research", time.time()))
            model = next(m for m in app.models() if m["id"] == "model-test")
            self.assertEqual(model["override"]["strengths"], "Good at tests")
            self.assertEqual(model["research"]["strengths"], ["Research strength"])


if __name__ == "__main__":
    unittest.main()
