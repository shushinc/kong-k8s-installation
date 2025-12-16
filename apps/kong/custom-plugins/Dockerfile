# Use the official Kong image as the base
FROM kong:latest

# Set user to root to have permissions to copy files
USER root

# Copy your custom plugin source code into the image
# Replace './your-plugin-name' with the actual path to your plugin's source code
COPY ./custom-log-plugin /usr/local/share/lua/5.1/kong/plugins/custom-log-plugin

# Set the necessary environment variables
# Add your plugin's name to the list of plugins to be loaded
ENV KONG_PLUGINS=bundled,custom-log-plugin
# Update the Lua package path to include your custom plugin
ENV KONG_LUA_PACKAGE_PATH=/usr/local/share/lua/5.1/?.lua;/usr/local/share/lua/5.1/?/init.lua;;

# Switch back to the kong user
USER kong