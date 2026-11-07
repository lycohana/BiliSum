if __name__ == "__main__":
    import sys

    if len(sys.argv) == 3 and sys.argv[1] == "--migrate-storage":
        from video_sum_service.storage_migration import run

        run(sys.argv[2])
    else:
        from video_sum_service.main import run

        run()
