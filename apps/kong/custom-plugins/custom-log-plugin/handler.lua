-- kong/plugins/custom-log-plugin/handler.lua
local cjson = require "cjson.safe"

local CustomLogPlugin = {
  PRIORITY = 10,
  VERSION  = "1.0",
}

-- Config defaults (optional; you can also put these into schema.lua)
local MAX_REQ_BODY = 128 * 1024     -- 128 KiB cap for request body
local MAX_RESP_BODY = 128 * 1024    -- 128 KiB cap for response body

local function is_json(ct)
  if not ct then return false end
  -- be lenient: application/json; charset=utf-8, +json, etc.
  return ct:find("application/json", 1, true) or ct:find("+json", 1, true)
end

-- Access phase: capture (small) request body if JSON
function CustomLogPlugin:access(conf)
  local ct = kong.request.get_header("content-type")
  if is_json(ct) then
    local raw = kong.request.get_raw_body()
    if raw and #raw > 0 then
      if #raw > MAX_REQ_BODY then
        raw = raw:sub(1, MAX_REQ_BODY)
      end
      kong.ctx.shared.request_body = raw
    end
  end
end

-- Body filter phase: accumulate response chunks
function CustomLogPlugin:body_filter(conf)
  local ctx = kong.ctx.shared
  -- Only bother buffering if response looks like JSON
  if ctx.__resp_json_checked ~= true then
    local rct = kong.response.get_header("content-type")
    ctx.__resp_is_json = is_json(rct)
    ctx.__resp_json_checked = true
  end
  if not ctx.__resp_is_json then
    return
  end

  local chunk = ngx.arg[1]
  local eof   = ngx.arg[2]

  if chunk and #chunk > 0 then
    local buf = ctx.response_body
    if buf then
      if #buf < MAX_RESP_BODY then
        local space_left = MAX_RESP_BODY - #buf
        if #chunk > space_left then
          buf = buf .. chunk:sub(1, space_left)
        else
          buf = buf .. chunk
        end
        ctx.response_body = buf
      end
    else
      if #chunk > MAX_RESP_BODY then
        ctx.response_body = chunk:sub(1, MAX_RESP_BODY)
      else
        ctx.response_body = chunk
      end
    end
  end

  if eof then
    ctx.__resp_complete = true
  end
end

-- Log phase: emit one JSON line to stdout
function CustomLogPlugin:log(conf)
  local ctx = kong.ctx.shared

  -- Extract carrierName from response JSON (if we captured it)
  local carrier_name = "Unknown"
  if ctx.__resp_is_json and ctx.response_body and ctx.__resp_complete then
    local ok, parsed = pcall(cjson.decode, ctx.response_body)
    if ok and parsed and type(parsed) == "table" then
      carrier_name = parsed.carrierName or parsed.carrier_name or "Unknown"
    end
  end

  -- Extract customerName from request JSON (if any)
  local customer_name = "Unknown"
  if ctx.request_body then
    local ok, parsed = pcall(cjson.decode, ctx.request_body)
    if ok and parsed and type(parsed) == "table" then
      customer_name = parsed.customerName or parsed.customer_name or "Unknown"
    end
  end

  -- Consumer
  local consumer = kong.client.get_consumer()
  local client = (consumer and (consumer.username or consumer.id)) or "Anonymous"

  -- Latency
  -- kong.request.get_start_time() returns ms since epoch
  local start_ms = kong.request.get_start_time()
  local start_s  = (type(start_ms) == "number") and (start_ms / 1000) or ngx.now()
  local latency_ms = (ngx.now() - start_s) * 1000

  -- URI: first path segment (e.g., "/number-verification/v0/..." -> "number-verification")
  local path = kong.request.get_path() or "/"
  local uri  = string.match(path, "^/([^/%s]+)") or "Unknown"

  local log_data = {
    timestamp     = os.date("!%Y-%m-%dT%H:%M:%SZ", start_s),
    status_code   = kong.response.get_status(),
    method        = kong.request.get_method(),
    uri           = uri,
    customer_name = customer_name,
    carrier_name  = carrier_name,
    client        = client,
    latency_ms    = latency_ms,
  }

  -- Emit ONLY pure JSON on a single line (great for DaemonSet shippers)
  local line, err = cjson.encode(log_data)
  if line then
    kong.log.notice(line)
  else
    -- fall back to a minimal line if encoding ever fails
    kong.log.notice(string.format('{"timestamp":"%s","status_code":%d,"method":"%s","uri":"%s"}',
      os.date("!%Y-%m-%dT%H:%M:%SZ", start_s),
      kong.response.get_status(),
      kong.request.get_method(),
      uri
    ))
  end
end

return CustomLogPlugin
