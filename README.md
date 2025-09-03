# CloudflareManager

A comprehensive multi-tenant Telegram bot for managing Cloudflare resources including DNS records and Cloudflare Tunnels.

## Features

### Core Features
- **Multi-tenant Architecture**: Support for multiple Cloudflare accounts with role-based access control
- **Role-based Access Control**: Super admins and tenant admins with appropriate permissions
- **DNS Management**: Complete CRUD operations for DNS records
- **Cloudflare Tunnel Management**: Full lifecycle management of Cloudflare tunnels
- **Domain Grouping**: Organize domains into logical groups for easier management

### DNS Management
- Create, read, update, and delete DNS records
- Support for all major record types (A, AAAA, CNAME, MX, TXT, SRV, NS, PTR)
- Bulk operations and domain grouping
- Real-time validation and error handling

### Cloudflare Tunnel Management 🚇
- **Create Tunnels**: Set up new Cloudflare tunnels with auto-generated secrets
- **View Tunnels**: Monitor tunnel status, connections, and configurations
- **Public Hostnames**: Add and manage public hostnames for applications
- **Private Networks**: Configure private network routing through tunnels
- **Delete Tunnels**: Clean removal of tunnels and their configurations

## Setup

### Prerequisites
- Python 3.13+
- Telegram Bot Token
- Cloudflare API Token with appropriate permissions

### Required Cloudflare API Permissions
For DNS management:
- `Zone:Zone:Read`
- `Zone:DNS:Edit`

For Tunnel management (additional):
- `Account:Cloudflare Tunnel:Edit`
- `Account:Cloudflare Tunnel:Read`
- `Zone:Zone Settings:Read`

### Installation

1. Clone the repository:
```bash
git clone <repository-url>
cd cloudflare-manager-telegram-bot
```

2. Install dependencies using uv:
```bash
uv install
```

3. Set environment variables:
```bash
export TELEGRAM_BOT_TOKEN="your_telegram_bot_token"
export DATABASE_URL="sqlite+aiosqlite:///dns_bot.db"  # Optional
export SUPER_ADMIN_ID="your_telegram_user_id"  # Optional
```

4. Run the bot:
```bash
uv run python main.py
```

## Usage

### Getting Started
1. Start the bot by sending `/start`
2. The first user becomes the super admin automatically
3. Super admins can add tenants and manage the bot
4. Tenant admins can manage their assigned Cloudflare accounts

### Tenant Management (Super Admin)
- Add new tenants with Cloudflare API tokens
- Assign tenant administrators
- View system statistics
- Manage bot configuration

### DNS Management
- Switch between tenants
- View and manage domains
- Create, edit, and delete DNS records
- Support for all DNS record types with validation

### Tunnel Management
- Create new Cloudflare tunnels
- Configure public hostnames for applications
- Set up private network routing
- Monitor tunnel status and connections
- Delete tunnels when no longer needed

## Tunnel Usage Examples

### Creating a Tunnel
1. Go to "🚇 Manage Tunnels"
2. Click "➕ Create New Tunnel"
3. Enter a descriptive name (e.g., "home-server")
4. Save the generated secret securely

### Adding Public Hostnames
1. Select your tunnel
2. Click "➕ Add Hostname"
3. Enter the subdomain (e.g., "app.example.com")
4. Enter the service URL (e.g., "http://localhost:8080")

### Setting Up Private Networks
1. Select your tunnel
2. Click "🌐 Add Network"
3. Enter the CIDR range (e.g., "192.168.1.0/24")

### Running Cloudflared
After creating a tunnel, run on your server:
```bash
# Install cloudflared
curl -L --output cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i cloudflared.deb

# Run your tunnel
cloudflared tunnel run your-tunnel-name
```

## Database Schema

The bot uses SQLite by default with the following tables:
- `tenants`: Store tenant information and API tokens
- `domain_groups`: Group domains for easier management
- `user_sessions`: Track user sessions and current context
- `bot_config`: Store bot configuration and settings

## Security Features

- **Role-based Access Control**: Super admins and tenant admins
- **Token Validation**: Cloudflare API tokens are validated on creation
- **Secure Storage**: API tokens are stored securely in the database
- **Access Logging**: All operations are logged for audit purposes

## Architecture

### Multi-tenant Design
- Each tenant represents a separate Cloudflare account
- Tenant admins can only access their assigned accounts
- Super admins have global access and can manage the bot

### Caching System
- Domains and tunnels are cached for performance
- Automatic cache refresh when switching tenants
- Manual refresh option available

### Error Handling
- Comprehensive error handling with user-friendly messages
- Automatic retry for transient failures
- Detailed logging for troubleshooting

## API Integration

The bot integrates with multiple Cloudflare APIs:

### DNS API
- `/zones` - List and manage DNS zones
- `/zones/{zone_id}/dns_records` - CRUD operations for DNS records

### Zero Trust Tunnels API
- `/accounts/{account_id}/cfd_tunnel` - Tunnel management
- `/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations` - Tunnel configuration
- `/accounts/{account_id}/cfd_tunnel/{tunnel_id}/connections` - Monitor connections

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests if applicable
5. Submit a pull request

## License

[Your chosen license]

## Support

For issues and questions:
1. Check the logs for error details
2. Verify Cloudflare API token permissions
3. Ensure Zero Trust is enabled for tunnel management
4. Create an issue in the repository

## Changelog

### v0.2.0 (Current)
- Added comprehensive Cloudflare Tunnel management
- Support for public hostnames and private networks
- Enhanced tenant caching with tunnel data
- Improved error handling and user feedback

### v0.1.0
- Initial release with DNS management
- Multi-tenant architecture
- Role-based access control
- Basic domain and record CRUD operations
