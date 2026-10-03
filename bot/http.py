import asyncio
import httpx


class APIError(RuntimeError):
    def __init__(self, service, message, *, transient=False, ambiguous=False, code=None):
        super().__init__(f"{service}: {message}")
        self.transient, self.ambiguous, self.code = transient, ambiguous, code


async def request(client, service, method, url, *, safe_retry=False, **kwargs):
    """Never replay a write after a transport error or an ambiguous HTTP 5xx.

    Retries are for reads, disposable generation, or explicit provider rejections.
    An uncertain publication must be reconciled instead of risking a duplicate.
    """
    for attempt in range(3):
        try:
            response = await client.request(method,url,**kwargs)
        except httpx.TransportError:
            if safe_retry and attempt < 2:
                await asyncio.sleep(2 ** attempt)
                continue
            raise APIError(service,"Connection failed. Check your internet connection.",transient=True,ambiguous=not safe_retry) from None
        try:
            data = response.json()
        except ValueError:
            data = {}
        error = data.get("error")
        if response.is_success and not error and data.get("ok",True):
            return data
        message = (error.get("message","") if isinstance(error,dict) else str(error or data.get("description","")))
        code = error.get("code") if isinstance(error,dict) else response.status_code
        explicit_transient = isinstance(error,dict) and (error.get("is_transient") or code in {1,2,4,17,32,613})
        transient = response.status_code in {429,500,502,503,504} or explicit_transient
        # 5xx writes can have committed remotely; even is_transient is insufficient.
        uncertain = response.status_code >= 500 and not safe_retry
        if transient and not uncertain and (safe_retry or explicit_transient or response.status_code == 429) and attempt < 2:
            delay = min(float(response.headers.get("Retry-After","0") or 0),30)
            await asyncio.sleep(max(delay,2 ** attempt))
            continue
        raise APIError(service,message or f"Request refused (HTTP {response.status_code}). Check account access and quota.",
                       transient=transient,ambiguous=uncertain,code=code)
