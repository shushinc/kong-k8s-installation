-- kong/plugins/custom-hello/handler.lua
local CustomHello = {
  PRIORITY = 1000,
  VERSION = "0.1.0",
}

function CustomHello:access(conf)
  -- Log a simple line with the request path
  kong.log.notice("[custom-hello] request path: ", kong.request.get_path())
end

function CustomHello:header_filter(conf)
  kong.response.set_header("X-Custom-Hello", conf.header_value or "hi-from-kong")
end

return CustomHello