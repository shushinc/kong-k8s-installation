-- kong/plugins/custom-hello/schema.lua
local typedefs = require "kong.db.schema.typedefs"

return {
  name = "custom-hello",
  fields = {
    { consumer = typedefs.no_consumer },  -- global/route/service plugin
    { protocols = typedefs.protocols_http },
    { config = {
        type = "record",
        fields = {
          { header_value = { type = "string", required = false, default = "hi-from-kong" } },
        },
      },
    },
  },
}