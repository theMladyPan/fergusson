import asyncio
import sys

from loguru import logger
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Footer, Header, Input, Markdown, Static

from src.broker.bus import MessageBus
from src.broker.schemas import InboundMessage, OutboundMessage


class UserMessage(Static):
    def __init__(self, text: str):
        content = Text.from_markup("[bold blue]You:[/bold blue] ")
        content.append(text)
        super().__init__(content)


class AgentMessage(Static):
    def __init__(self, text: str):
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        yield Static("[bold green]Fergusson:[/bold green]", classes="agent-header")
        yield Markdown(self.text, classes="agent-content")


class FergussonCLI(App):
    CSS = """
    UserMessage {
        margin: 1 2;
        padding: 1 2;
        background: $boost;
        color: $text;
        text-align: right;
        border: round $primary;
    }

    AgentMessage {
        margin: 1 2;
        padding: 1 2;
        background: $surface;
        border: round $secondary;
    }

    .agent-header {
        margin-bottom: 1;
    }

    .agent-content {
        margin-left: 1;
    }

    #message-input {
        dock: bottom;
        margin: 1;
    }

    #chat-container {
        height: 1fr;
    }
    """

    def __init__(self, user_id: str = "cli_user", username: str = "CLI User"):
        super().__init__()
        self.bus = MessageBus()
        self.user_id = user_id
        self.username = username
        self.chat_id = "cli_chat"
        self.channel_name = "cli"
        self._pubsub = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(id="chat-container")
        yield Input(placeholder="Type your message here... (/quit to exit)", id="message-input")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Fergusson CLI"
        self.sub_title = "Omnipotent Personal Assistant"
        self.query_one("#message-input", Input).focus()
        self.listen_for_replies()

    @work(exclusive=True, thread=False)
    async def listen_for_replies(self):
        self._pubsub = await self.bus.subscribe_outbound(self.channel_name)
        try:
            async for message in self._pubsub.listen():
                if message["type"] == "message":
                    try:
                        msg = OutboundMessage.model_validate_json(message["data"])
                        # Mount the agent message
                        container = self.query_one("#chat-container", VerticalScroll)
                        agent_msg = AgentMessage(msg.content)
                        await container.mount(agent_msg)
                        agent_msg.scroll_visible()
                    except Exception as e:
                        logger.error(f"Failed to process outbound message: {e}")
        except asyncio.CancelledError:
            pass
        finally:
            if self._pubsub:
                await self._pubsub.unsubscribe(self.channel_name)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        input_widget = event.input
        content = input_widget.value.strip()
        input_widget.value = ""

        if not content:
            return

        if content.lower() in ["/quit", "/exit"]:
            self.exit()
            return

        # Display user message
        container = self.query_one("#chat-container", VerticalScroll)
        user_msg = UserMessage(content)
        await container.mount(user_msg)
        user_msg.scroll_visible()

        # Send to bus
        msg = InboundMessage(
            sender_id=self.user_id,
            username=self.username,
            chat_id=self.chat_id,
            content=content,
            channel=self.channel_name,
            metadata={},
        )
        asyncio.create_task(self.bus.publish_inbound(msg))


if __name__ == "__main__":
    # Suppress verbose loguru output in CLI mode so it doesn't break Textual rendering
    # Instead, dump logs to a local file for debugging if necessary.
    logger.remove()
    logger.add("workspace/cli.log", format="{time} {level} {message}", level="DEBUG")

    app = FergussonCLI()
    app.run()