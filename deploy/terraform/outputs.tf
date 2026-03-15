output "service_url" {
  description = "Cloud Run service URL"
  value       = google_cloud_run_v2_service.backend.uri
}

output "websocket_url" {
  description = "WebSocket endpoint URL"
  value       = "${replace(google_cloud_run_v2_service.backend.uri, "https://", "wss://")}/ws"
}

output "health_url" {
  description = "Health check URL"
  value       = "${google_cloud_run_v2_service.backend.uri}/health"
}
