import uvicorn


def test_native_api_keeps_serving_after_ten_thousand_requests(monkeypatch):
    from app import server as entrypoint

    real_server = uvicorn.Server
    checked = []

    class ProbeServer:
        def __init__(self, settings):
            self.server = real_server(settings)

        async def serve(self):
            self.server.server_state.total_requests = 10000
            assert await self.server.on_tick(1) is False
            self.server.should_exit = True
            assert await self.server.on_tick(1) is True
            checked.append(True)

    monkeypatch.setattr(entrypoint.uvicorn, "Server", ProbeServer)
    entrypoint.main()
    assert checked == [True]
