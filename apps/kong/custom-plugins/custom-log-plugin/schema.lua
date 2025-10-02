local typedefs = require "kong.db.schema.typedefs"

return {
  name = "custom-log-plugin",
  fields = {
    { consumer = typedefs.no_consumer },  -- Plugin does not apply to specific consumers
    { config = {
        type = "record",
        fields = {
            { log_method = {
                type = "string",
                default = "POST",
                required = true,
                one_of = { "GET", "POST", "PUT", "PATCH", "DELETE" },
                description = "The HTTP method to log (e.g., POST, GET)."
            }}, -- Optional field for log method
            { log_status_code = {
                type = "integer",
                default = 401,
                required = true,
                description = "The HTTP status code to log."
            }}, -- Optional field for log status code
        },
      },
    },
  },
}
