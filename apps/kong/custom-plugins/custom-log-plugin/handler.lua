local cjson = require "cjson"  -- Import JSON module for decoding
local CustomLogPlugin = {
  PRIORITY = 10,
  VERSION = "1.0",
}

-- Access phase: capture the request body
function CustomLogPlugin:access(conf)
  local raw_request_body = kong.request.get_raw_body()
  if raw_request_body then
    kong.ctx.shared.request_body = raw_request_body  -- Save request body for log phase
  end
end

-- Body filter phase: capture the response body
function CustomLogPlugin:body_filter(conf)
  local body = kong.response.get_raw_body()
  if body then
    kong.ctx.shared.response_body = body  -- Save response body for log phase
  end
end

-- Log phase: handle traffic logging based on configuration
function CustomLogPlugin:log(conf)
  local request_method = kong.request.get_method()
  local request_uri = kong.request.get_path() or "root"  -- Default to "root" if URI is empty
  local first_part_of_uri = string.match(request_uri, "^/([^/]+)") or "Unknown"
  
  local response_status = kong.response.get_status()  -- Get response status
  local api_key = kong.request.get_query_arg("apikey") or kong.request.get_header("apikey") or "Unknown API Key"
  
  local carrier_name = "Unknown"
  local customer_name = "Unknown"  -- Initialize customer_name
  
  -- Extract carrierName from response body
  local raw_body = kong.ctx.shared.response_body
  if raw_body then
    local success, parsed_body = pcall(cjson.decode, raw_body)
    if success and parsed_body then
      carrier_name = parsed_body.carrierName or "Unknown"
    else
      kong.log.err("Failed to decode response body")
    end
  else
    kong.log.err("Response body is empty or unavailable")
  end

  -- Extract customerName from request body
  local raw_request_body = kong.ctx.shared.request_body
  if raw_request_body then
    local success, parsed_body = pcall(cjson.decode, raw_request_body)
    if success and parsed_body then
      customer_name = parsed_body.customerName or "Unknown"  -- Extract customerName
    else
      kong.log.err("Failed to decode request body")
    end
  else
    kong.log.err("Request body is empty or unavailable")
  end

  local consumer = kong.client.get_consumer()
  local client = consumer and consumer.username or "Anonymous"
  local kong_start_time = kong.request.get_start_time() / 1000  -- Convert to seconds
  local kong_end_time = ngx.now()  -- Current time in seconds
  local latency_ms = (kong_end_time - kong_start_time) * 1000  -- Calculate latency in milliseconds
  local readable_timestamp = os.date("%Y-%m-%d %H:%M:%S", kong_start_time)

  local log_entry = string.format(
    "Timestamp: %s, Status: %d, Method: %s, Attribute: %s, Carrier Name: %s, Customer Name: %s, Client: %s, Latency: %.2f ms",
    readable_timestamp, response_status, request_method, first_part_of_uri, carrier_name, customer_name, client, latency_ms
  )
  
  local log_file_name = "/usr/local/kong/logs/custom_api_transaction.log"
  local file = io.open(log_file_name, "a")
  if file then
    file:write(log_entry .. "\n")
    file:close()
  else
    kong.log.err("Could not open log file: " .. log_file_name)
  end
end

return CustomLogPlugin
